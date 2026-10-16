"""Candado de proceso único para el scheduler (Paso 11c; principio P5).

Dos schedulers vivos a la vez (por ejemplo un contenedor que se recrea mientras
el anterior todavía no murió, o alguien que lo lanza a mano) duplicarían ciclos
de análisis y gasto de IA, y harían que cada uno marcara como "interrumpido" el
ciclo del otro. El candado es un `pg_try_advisory_lock` de SESIÓN sostenido en
una conexión dedicada durante toda la vida del proceso: si el proceso muere, la
conexión se cae y Postgres lo libera solo. No hace falta ningún archivo ni
limpieza al arrancar.

Excepción de capa: a diferencia del resto de repositorios, este recibe un
`Engine` y no una `Session`, porque un candado de sesión debe vivir en una
conexión propia que NO se devuelva al pool mientras dure el proceso.
"""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

_LOCK_KEY_SCHEDULER = 8_200_410_004


class SchedulerLock:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection: Connection | None = None

    @property
    def held(self) -> bool:
        return self._connection is not None

    def try_acquire(self) -> bool:
        """`True` si este proceso quedó como único scheduler; `False` si otro
        ya lo es. Idempotente: pedirlo de nuevo estando tomado devuelve `True`."""
        if self._connection is not None:
            return True
        connection = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            obtenido = bool(
                connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY_SCHEDULER}).scalar_one()
            )
        except Exception:
            connection.close()
            raise
        if not obtenido:
            connection.close()
            return False
        self._connection = connection
        return True

    def release(self) -> None:
        if self._connection is None:
            return
        try:
            self._connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY_SCHEDULER})
        finally:
            self._connection.close()
            self._connection = None
