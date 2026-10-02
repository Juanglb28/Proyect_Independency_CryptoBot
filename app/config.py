"""Configuración tipada de la aplicación.

Toda la configuración pasa por este módulo. Nada en el resto del código debe
leer variables de entorno directamente (os.environ / os.getenv): esto es lo
único que permite validar la configuración al arrancar y fallar de forma
explícita si falta algo obligatorio, en vez de fallar más tarde con un error
críptico en medio de una operación.

Importante (sección 15.4 del plan): las reglas de riesgo críticas (stop loss,
take profit, trailing, drawdown máximo) NUNCA se modelan aquí ni se leen desde
`.env`. Esas viven como constantes en el código de `domain/rules/risk.py`
(todavía no implementado en este primer entregable), con cobertura de tests
al 100%. Lo que sí es configuración legítima por entorno es el capital
simulado y el número máximo de posiciones, que sí aparecen abajo.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuración de la aplicación, cargada desde variables de entorno o `.env`.

    Los campos sin valor por defecto son obligatorios: si faltan, la
    aplicación debe fallar al arrancar (ver `get_settings()` más abajo), no
    en el primer punto del código que intente usarlos.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Obligatoria: sin base de datos no hay nada que arrancar ---
    database_url: str = Field(
        description="Cadena de conexión de SQLAlchemy a PostgreSQL, "
        "ej. postgresql+psycopg://user:pass@localhost:5432/crypto_trading",
    )

    # --- Opcional: solo la usan los tests, nunca la aplicación en marcha ---
    test_database_url: str | None = Field(
        default=None,
        description="Cadena de conexión a la base de datos de PRUEBAS, "
        "completamente separada de `database_url` para que los tests de "
        "integración nunca lean ni escriban sobre datos reales. Si no se "
        "define, `tests/conftest.py` la deriva automáticamente de "
        "`database_url` agregando el sufijo `_test` al nombre de la base "
        "(ver ese módulo para el porqué: un bug real donde los tests "
        "colisionaron con velas ya backfillizadas de verdad).",
    )

    # --- Binance: no se usa todavía en este entregable (Fase 1 en adelante) ---
    binance_api_key: str | None = Field(default=None)
    binance_api_secret: str | None = Field(default=None)
    binance_testnet: bool = Field(
        default=True,
        description="Debe permanecer en True hasta la Fase 6 (capital real). "
        "Ver sección 10 del plan.",
    )

    # --- Anthropic: no se usa todavía (Fase 3 en adelante) ---
    anthropic_api_key: str | None = Field(default=None)

    # --- Control de gastos de la API de Anthropic (bug real 2026-10-02:
    # ver app/ai/models.py::calculate_cost_usd y README "Control de gastos
    # de la API de Anthropic") ---
    anthropic_saldo_declarado_usd: Decimal | None = Field(
        default=None,
        description="Saldo real de la cuenta de Anthropic (consola -> "
        "Billing -> Credit balance), a la fecha de "
        "`anthropic_saldo_declarado_en`. Dato declarado por el usuario, "
        "NUNCA inferido ni inventado (sección P3 del plan) — la API de "
        "Anthropic no expone un endpoint de saldo real. Se usa solo para "
        "ESTIMAR cuánto queda, sumando el gasto registrado en "
        "`llamadas_ia` desde esa fecha — una estimación que diverge del "
        "saldo real por cualquier gasto que este sistema no haya podido "
        "registrar (llamadas fallidas, otra API key compartiendo el "
        "mismo saldo, etc.). Si no se define, el reporte de cada corrida "
        "simplemente omite el saldo restante estimado.",
    )
    anthropic_saldo_declarado_en: datetime | None = Field(
        default=None,
        description="Fecha/hora (ISO 8601, con offset de zona horaria — "
        "ej. 2026-10-02T00:00:00+00:00) en la que el saldo de arriba era "
        "correcto, según la consola de Anthropic. Recalibrar ambos valores "
        "cada vez que se revise el saldo real mantiene la estimación "
        "cerca de la realidad.",
    )

    # --- Telegram: no se usa todavía (Fase 4 en adelante) ---
    telegram_bot_token: str | None = Field(default=None)
    telegram_chat_id: str | None = Field(default=None)

    # --- Parámetros operativos de paper trading (sección 9 del plan) ---
    paper_capital_usd: Decimal = Field(
        default=Decimal("1000"),
        description="Capital simulado inicial. Fijado en el plan en 1.000 USD.",
    )
    max_open_positions: int = Field(
        default=4,
        description="Máximo de posiciones simultáneas. Fijado en el plan en 4.",
    )


def get_settings() -> Settings:
    """Construye y valida la configuración.

    Se llama explícitamente (nunca como singleton global mutable) para que
    los tests puedan inyectar su propia configuración sin depender de
    variables de entorno del proceso.
    """
    return Settings()
