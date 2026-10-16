"""Sincroniza los datos de una base a otra con el MISMO esquema (Paso 11-0;
sincronización re-ejecutable, tarea #68, 2026-10-13).

Para qué: mantener un respaldo/espejo (p. ej. en Railway) al día. A diferencia
de la primera versión (un solo uso, destino vacío), esta se puede re-ejecutar:
no exige destino vacío y converge el destino hacia el origen en cada corrida.

Por qué no `pg_dump`/`pg_restore`: `velas` es una hipertabla de TimescaleDB y
restaurar sus datos con `pg_dump` exige la misma versión de la extensión y
pasos especiales. `COPY` tabla por tabla funciona igual en cualquier versión.

Estrategia por tabla (se decide sola según la tabla; decisión del usuario
2026-10-13):
- Tablas INMUTABLES (las que tienen un trigger BEFORE UPDATE que rechaza
  cambios: velas cerradas, trade_results, trade_snapshots, alertas,
  equity_observaciones, eventos_sistema, perfil_usuario, prefiltro_resultados):
  `INSERT … ON CONFLICT DO NOTHING`. Solo se agregan filas nuevas; las ya
  copiadas no cambian, así que nunca fallan ni se desvían.
- Tablas MUTABLES (posiciones, ordenes, performance_metrics, …): `UPSERT`
  (`ON CONFLICT (pk) DO UPDATE`), para reflejar los cambios de estado.
- `velas`: solo las CERRADAS. La vela en curso no se copia hasta que cierra
  (así nunca choca con su trigger de inmutabilidad); entra en la siguiente
  corrida ya definitiva.

Garantías (cualquier fallo = no se escribe NADA):
- Las dos bases deben estar en la MISMA migración de Alembic y tener las
  mismas tablas.
- Todo el destino se escribe en UNA transacción; solo se confirma si, al final,
  el destino es SUPERCONJUNTO del origen (ninguna fila del origen falta).
- El origen se lee en una transacción de solo lectura con foto consistente
  (REPEATABLE READ): un monitor escribiendo a la vez no genera inconsistencias,
  pero conviene pararlo (ver README).
- Los ids se preservan tal cual (OVERRIDING SYSTEM VALUE en columnas de
  identidad) para no romper claves foráneas; las secuencias se reajustan.
- Nunca imprime las URLs (llevan contraseña).

Limitaciones declaradas: el recibo de entrega de `alertas` y la vela en curso
no se reflejan en el espejo (ambas menores).

Sin `--ejecutar` solo informa qué haría (no escribe nada).

Uso (variables de entorno, ver README):
    python -m scripts.copy_database            # informe, no escribe
    python -m scripts.copy_database --ejecutar # sincroniza
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


# Filtro por tabla: qué filas se sincronizan (el resto, todas).
_FILTRO_FILAS: dict[str, str] = {
    "velas": "cerrada = true",  # la vela en curso se copia cuando cierre
}


def _tablas_inmutables(conn: psycopg.Connection) -> set[str]:
    """Tablas con un trigger BEFORE UPDATE a nivel de fila: sus filas no se
    actualizan, así que se sincronizan con ON CONFLICT DO NOTHING."""
    filas = conn.execute(
        "SELECT DISTINCT event_object_table FROM information_schema.triggers "
        "WHERE trigger_schema = 'public' AND event_manipulation = 'UPDATE' AND action_timing = 'BEFORE'"
    ).fetchall()
    return {f[0] for f in filas}


def _columnas_pk(conn: psycopg.Connection, tabla: str) -> list[str]:
    """Columnas de la clave primaria, en orden (objetivo del ON CONFLICT)."""
    filas = conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)",
        (f'public."{tabla}"',),
    ).fetchall()
    return [f[0] for f in filas]


def _columnas_identidad(conn: psycopg.Connection, tabla: str) -> set[str]:
    filas = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND is_identity = 'YES'",
        (tabla,),
    ).fetchall()
    return {f[0] for f in filas}


def _contar_filtrado(conn: psycopg.Connection, tabla: str) -> int:
    filtro = _FILTRO_FILAS.get(tabla)
    consulta = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(tabla))
    if filtro:
        consulta = consulta + sql.SQL(" WHERE ") + sql.SQL(filtro)  # filtro es una constante del código, no entrada
    return conn.execute(consulta).fetchone()[0]


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


def _sincronizar_tabla(
    origen: psycopg.Connection,
    destino: psycopg.Connection,
    tabla: str,
    cols: list[str],
    *,
    inmutable: bool,
) -> None:
    """Sincroniza UNA tabla: COPY del origen a una temporal del destino y luego
    INSERT … SELECT … ON CONFLICT (DO NOTHING si es inmutable, DO UPDATE si no).
    Preserva los ids (OVERRIDING SYSTEM VALUE en columnas de identidad)."""
    t = sql.Identifier(tabla)
    lista = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    filtro = _FILTRO_FILAS.get(tabla)
    where = (sql.SQL(" WHERE ") + sql.SQL(filtro)) if filtro else sql.SQL("")

    destino.execute(sql.SQL("DROP TABLE IF EXISTS _sync_tmp"))
    destino.execute(sql.SQL("CREATE TEMP TABLE _sync_tmp AS SELECT {} FROM {} WITH NO DATA").format(lista, t))
    with origen.cursor().copy(sql.SQL("COPY (SELECT {} FROM {}{}) TO STDOUT").format(lista, t, where)) as salida:
        with destino.cursor().copy(sql.SQL("COPY _sync_tmp ({}) FROM STDIN").format(lista)) as entrada:
            for bloque in salida:
                entrada.write(bloque)

    overriding = sql.SQL("OVERRIDING SYSTEM VALUE ") if (_columnas_identidad(destino, tabla) & set(cols)) else sql.SQL("")
    if inmutable:
        conflicto = sql.SQL("ON CONFLICT DO NOTHING")
    else:
        pk = _columnas_pk(destino, tabla)
        if not pk:
            raise CopiaAbortada(f"La tabla mutable {tabla} no tiene clave primaria: no se puede hacer UPSERT.")
        set_cols = [c for c in cols if c not in pk]
        asignaciones = sql.SQL(", ").join(
            sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in set_cols
        )
        destino_pk = sql.SQL(", ").join(sql.Identifier(c) for c in pk)
        conflicto = (
            sql.SQL("ON CONFLICT ({}) DO UPDATE SET ").format(destino_pk) + asignaciones
            if set_cols
            else sql.SQL("ON CONFLICT ({}) DO NOTHING").format(destino_pk)
        )
    destino.execute(
        sql.SQL("INSERT INTO {} ({}) {}SELECT {} FROM _sync_tmp {}").format(t, lista, overriding, lista, conflicto)
    )
    destino.execute(sql.SQL("DROP TABLE _sync_tmp"))
    _ajustar_secuencias(destino, tabla, cols)


def copiar(origen: psycopg.Connection, destino: psycopg.Connection, *, ejecutar: bool) -> ResultadoCopia:
    """Verifica y (si `ejecutar`) sincroniza. Lanza `CopiaAbortada` ante cualquier problema."""
    rev_o, rev_d = _revision(origen), _revision(destino)
    if rev_o is None or rev_o != rev_d:
        raise CopiaAbortada(f"Las bases no están en la misma migración (origen: {rev_o}, destino: {rev_d}). "
                            "Aplica `alembic upgrade head` en ambas.")
    tablas_o, tablas_d = _tablas(origen), _tablas(destino)
    if tablas_o != tablas_d:
        raise CopiaAbortada(f"Las tablas no coinciden. Solo en origen: {sorted(set(tablas_o) - set(tablas_d))}; "
                            f"solo en destino: {sorted(set(tablas_d) - set(tablas_o))}.")

    orden = _orden_por_dependencias(destino, tablas_d)
    inmutables = _tablas_inmutables(destino)
    destino.rollback()
    origen.rollback()  # cierra la transacción abierta por las lecturas previas
    origen.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")  # foto consistente
    esperadas = {t: _contar_filtrado(origen, t) for t in orden}
    if not ejecutar:
        origen.rollback()
        return ResultadoCopia(esperadas, ejecutada=False)

    try:
        for tabla in orden:
            cols = _columnas(origen, tabla)
            if cols != _columnas(destino, tabla):
                raise CopiaAbortada(f"Las columnas de {tabla} no coinciden entre origen y destino.")
            _sincronizar_tabla(origen, destino, tabla, cols, inmutable=tabla in inmutables)

        # Verificación: el destino debe ser SUPERCONJUNTO del origen (ninguna
        # fila del origen falta). Conteo por tabla: destino >= origen.
        destino_cuenta = {t: _contar(destino, t) for t in orden}
        faltan = {t: (esperadas[t], destino_cuenta[t]) for t in orden if destino_cuenta[t] < esperadas[t]}
        if faltan:
            raise CopiaAbortada(f"Faltan filas en el destino (origen, destino): {faltan}. No se confirmó nada.")
        destino.commit()
    except BaseException:
        destino.rollback()
        raise
    finally:
        origen.rollback()
    return ResultadoCopia(destino_cuenta, ejecutada=True)


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
    print(
        "Sincronizado y verificado (filas en el destino):"
        if r.ejecutada
        else "SIMULACIÓN (nada escrito). Filas del origen que se sincronizarían:"
    )
    print("\n".join(f"  {t:<{ancho}}  {n}" for t, n in r.filas.items()))
    print(f"Total: {sum(r.filas.values())} filas." + ("" if r.ejecutada else " Repite con --ejecutar para sincronizar."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
