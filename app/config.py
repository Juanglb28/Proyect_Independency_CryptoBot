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
