"""Copia TODOS los datos de una base a otra con el MISMO esquema (Paso 11-0).

Para qué: pasar el historial de tu base local a la de Railway una sola vez.
Por qué no `pg_dump`/`pg_restore`: `velas` es una hipertabla de TimescaleDB y
restaurar sus datos con `pg_dump` exige la misma versión de la extensión y
pasos especiales. `COPY` tabla por tabla funciona igual en cualquier versión.

Garantías (cualquier fallo = no se copia NADA):
- Las dos bases deben estar en la MISMA migración de Alembic y tener las
  mismas tablas.
- El destino debe estar VACÍO: nunca sobrescribe ni mezcla datos.
- Todo el destino se escribe en UNA transacción; solo se confirma si el
  conteo de filas de cada tabla coincide con el origen.
- El origen se lee en una transacción de solo lectura con foto consistente
  (REPEATABLE READ): un monitor escribiendo a la vez no genera inconsistencias,
  pero conviene pararlo (ver README).
- Las secuencias de identidad se reajustan para que los próximos ids no choquen.
- Nunca imprime las URLs (llevan contraseña).

Sin `--ejecutar` solo informa qué haría (no escribe nada).

Uso (variables de entorno, ver README):
    python -m scripts.copy_database            # informe, no escribe
    python -m scripts.copy_database --ejecutar # copia
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ENV_ORIGEN = "SOURCE_DATABASE_URL"
ENV_DESTINO = "DEST_DATABASE_URL"


class CopiaAbortada(Exception):
    """Una verificación previa o posterior falló: no se confirmó nada."""


@dataclass(frozen=True)
class ResultadoCopia:
    filas: dict[str, int]
    ejecutada: bool


def conectar(url: str) -> psycopg.Connection:
    """Conexión psycopg desde una URL de SQLAlchemy (`postgresql+psycopg://…`)."""
    return psycopg.connect(make_url(url).set(drivername="postgresql").render_as_string(hide_password=False))


def _tablas(conn: psycopg.Connection) -> list[str]:
    filas = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' ORDER BY tablename"
    ).fetchall()
    return [f[0] for f in filas]


def _revision(conn: psycopg.Connection) -> str | None:
    try:
        fila = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        return None
    return fila[0] if fila else None


def _orden_por_dependencias(conn: psycopg.Connection, tablas: list[str]) -> list[str]:
    """Padres antes que hijos (claves foráneas). Las autorreferencias se ignoran;
    un ciclo entre tablas distintas aborta (no se sabría en qué orden cargar)."""
    pendientes = {t: set() for t in tablas}
    for hijo, padre in conn.execute(
        "SELECT c.conrelid::regclass::text, c.confrelid::regclass::text FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace WHERE c.contype = 'f' AND n.nspname = 'public'"
    ).fetchall():
        hijo, padre = hijo.strip('"'), padre.strip('"')
        if hijo in pendientes and padre in pendientes and hijo != padre:
            pendientes[hijo].add(padre)
    orden: list[str] = []
    while pendientes:
        listas = sorted(t for t, deps in pendientes.items() if not deps)
        if not listas:
            raise CopiaAbortada("Hay un ciclo de claves foráneas entre tablas: no se puede decidir el orden de carga.")
        for t in listas:
            orden.append(t)
            del pendientes[t]
        for deps in pendientes.values():
            deps.difference_update(listas)
    return orden


def _columnas(conn: psycopg.Connection, tabla: str) -> list[str]:
    """Columnas copiables, en orden. Se excluyen las generadas (se recalculan)."""
    filas = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
        "AND is_generated = 'NEVER' ORDER BY ordinal_position",
        (tabla,),
    ).fetchall()
    return [f[0] for f in filas]


def _contar(conn: psycopg.Connection, tabla: str) -> int:
    return conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(tabla))).fetchone()[0]


def _ajustar_secuencias(conn: psycopg.Connection, tabla: str, columnas: list[str]) -> None:
    for col in columnas:
        seq = conn.execute("SELECT pg_get_serial_sequence(%s, %s)", (f'public."{tabla}"', col)).fetchone()[0]
        if seq is None:
            continue
        conn.execute(
            sql.SQL("SELECT setval(%s, COALESCE(max({c}), 1), max({c}) IS NOT NULL) FROM {t}").format(
                c=sql.Identifier(col), t=sql.Identifier(tabla)
            ),
            (seq,),
        )


def copiar(origen: psycopg.Connection, destino: psycopg.Connection, *, ejecutar: bool) -> ResultadoCopia:
    """Verifica y (si `ejecutar`) copia. Lanza `CopiaAbortada` ante cualquier problema."""
    rev_o, rev_d = _revision(origen), _revision(destino)
    if rev_o is None or rev_o != rev_d:
        raise CopiaAbortada(f"Las bases no están en la misma migración (origen: {rev_o}, destino: {rev_d}). "
                            "Aplica `alembic upgrade head` en ambas.")
    tablas_o, tablas_d = _tablas(origen), _tablas(destino)
    if tablas_o != tablas_d:
        raise CopiaAbortada(f"Las tablas no coinciden. Solo en origen: {sorted(set(tablas_o) - set(tablas_d))}; "
                            f"solo en destino: {sorted(set(tablas_d) - set(tablas_o))}.")
    llenas = {t: n for t in tablas_d if (n := _contar(destino, t)) > 0}
    if llenas:
        raise CopiaAbortada(f"El destino NO está vacío (tablas con filas: {sorted(llenas)}). No se copia nada.")
    destino.rollback()

    orden = _orden_por_dependencias(destino, tablas_d)
    origen.rollback()
    origen.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")  # foto consistente
    esperadas = {t: _contar(origen, t) for t in orden}
    if not ejecutar:
        origen.rollback()
        return ResultadoCopia(esperadas, ejecutada=False)

    try:
        for tabla in orden:
            cols = _columnas(origen, tabla)
            if cols != _columnas(destino, tabla):
                raise CopiaAbortada(f"Las columnas de {tabla} no coinciden entre origen y destino.")
            lista = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
            t = sql.Identifier(tabla)
            with origen.cursor().copy(sql.SQL("COPY (SELECT {} FROM {}) TO STDOUT").format(lista, t)) as salida:
                with destino.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(t, lista)) as entrada:
                    for bloque in salida:
                        entrada.write(bloque)
            _ajustar_secuencias(destino, tabla, cols)
        copiadas = {t: _contar(destino, t) for t in orden}
        if copiadas != esperadas:
            malas = {t: (esperadas[t], copiadas[t]) for t in orden if esperadas[t] != copiadas[t]}
            raise CopiaAbortada(f"Los conteos no coinciden (origen, destino): {malas}. No se confirmó nada.")
        destino.commit()
    except BaseException:
        destino.rollback()
        raise
    finally:
        origen.rollback()
    return ResultadoCopia(copiadas, ejecutada=True)


def main(argv: list[str] | None = None) -> int:
    ejecutar = "--ejecutar" in (sys.argv[1:] if argv is None else argv)
    url_o, url_d = os.environ.get(ENV_ORIGEN), os.environ.get(ENV_DESTINO)
    if not url_o or not url_d:
        print(f"Faltan las variables de entorno {ENV_ORIGEN} y {ENV_DESTINO}. No se hizo ningún cambio.")
        return 2
    if make_url(url_o).render_as_string(hide_password=False) == make_url(url_d).render_as_string(hide_password=False):
        print("Origen y destino son la misma base. No se hizo ningún cambio.")
        return 2
    try:
        with conectar(url_o) as origen, conectar(url_d) as destino:
            r = copiar(origen, destino, ejecutar=ejecutar)
    except CopiaAbortada as exc:
        print(f"ABORTADO: {exc}")
        return 1
    except psycopg.Error as exc:
        print(f"ABORTADO por error de base de datos ({type(exc).__name__}: {str(exc).splitlines()[0]}); no se confirmó nada.")
        return 1
    ancho = max((len(t) for t in r.filas), default=0)
    print(("Copiado y verificado:" if r.ejecutada else "SIMULACIÓN (nada escrito). Filas que se copiarían:"))
    print("\n".join(f"  {t:<{ancho}}  {n}" for t, n in r.filas.items()))
    print(f"Total: {sum(r.filas.values())} filas." + ("" if r.ejecutada else " Repite con --ejecutar para copiar."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
