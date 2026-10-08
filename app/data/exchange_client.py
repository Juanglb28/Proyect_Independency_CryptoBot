"""Cliente mínimo de Binance para velas históricas (Fase 0 / Paso 3 del plan).

Alcance deliberadamente mínimo: solo lectura de velas (OHLCV) públicas.
No coloca órdenes, no consulta balance, no firma peticiones. Ver sección
29 del master prompt ("no sobreimplementación") — el motor de órdenes es
un bloque posterior y separado (capa `execution`, nunca importada desde
aquí).

Mainnet, no testnet, para los datos de mercado
------------------------------------------------
Este cliente apunta siempre a Binance **mainnet** para velas, aunque
`BINANCE_TESTNET=true` sea el valor por defecto en `.env` (esa variable
gobierna las credenciales de cuenta que se usarán más adelante para
simular envío de órdenes en la Fase 4, no la lectura de mercado).

Razón (decidida con el usuario el 2026-09-19): el testnet de Binance no
tiene el historial de ~2 años que exige la sección 3 del plan para los
20 pares de la whitelist, y su libro de órdenes es sintético — no refleja
precios ni volumen reales. Calcular indicadores sobre datos de testnet
invalidaría cualquier intento posterior de medir si el score tiene
capacidad predictiva sobre el mercado real. El endpoint de velas de
Binance es público y de solo lectura: no requiere API key ni mueve
dinero, así que usar mainnet aquí no contradice "no conectar dinero real
en esta fase" (sección 37, restricción 11).

Símbolos y temporalidades
--------------------------
Solo se aceptan símbolos de `app.domain.symbols.ALLOWED_SYMBOLS` (sección
21 del plan: lista explícita y controlada, nunca dinámica) y las
temporalidades ya decididas en la sección 6.1 del plan (1h, 4h, 12h, 1d).
Pedir cualquier otro valor lanza un error explícito en vez de intentar
adivinar o normalizar silenciosamente (sección 3 del master prompt:
"cero datos inventados").
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import NotRequired, TypedDict

import ccxt

from app.domain.symbols import ALLOWED_SYMBOLS

# Temporalidades decididas en la sección 6.1 del plan para el cálculo de
# indicadores. No confundir con `limit`/paginación: esto es qué velas
# tiene sentido pedir, no cuántas.
SUPPORTED_TIMEFRAMES: tuple[str, ...] = ("1h", "4h", "12h", "1d")

# Límite máximo por llamada que acepta la API de Binance para /klines.
_MAX_LIMIT_PER_CALL = 1000


class InvalidSymbolError(ValueError):
    """El símbolo pedido no está en la whitelist (sección 21 del plan)."""


class InvalidTimeframeError(ValueError):
    """La temporalidad pedida no es una de las decididas en la sección 6.1."""


class Candle(TypedDict):
    """Una vela cruda, lista para pasar a `Vela` (app/db/models/candles.py).

    Los precios y el volumen ya vienen como `Decimal`, nunca `float`
    (sección 15.3 del plan: nunca float para persistencia financiera).
    """

    symbol: str
    timeframe: str
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    # `True` = vela ya cerrada (valores definitivos), `False` = en curso (sus
    # valores todavía cambian). Lo anota la ingesta (`candle_ingestion`) con
    # `app.domain.rules.candles.esta_cerrada`; `fetch_candles` no lo sabe y no
    # lo pone. Si falta, el repositorio la trata como cerrada (datos históricos
    # de pruebas); la ingesta real siempre la anota.
    cerrada: NotRequired[bool]


def _validate_symbol(symbol: str) -> None:
    if symbol not in ALLOWED_SYMBOLS:
        raise InvalidSymbolError(
            f"Símbolo no autorizado: {symbol!r}. "
            f"Ver la whitelist en app/domain/symbols.py (sección 21 del plan)."
        )


def _validate_timeframe(timeframe: str) -> None:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidTimeframeError(
            f"Temporalidad no soportada: {timeframe!r}. "
            f"Usar una de {SUPPORTED_TIMEFRAMES} (sección 6.1 del plan)."
        )


def _to_ccxt_symbol(symbol: str) -> str:
    """Convierte 'BTCUSDT' (formato de almacenamiento, sección 6) al
    formato 'BTC/USDT' que espera ccxt. Todos los símbolos de la whitelist
    cotizan contra USDT, así que el corte de los últimos 4 caracteres es
    seguro aquí; si eso cambiara habría que revisar esta función."""
    if not symbol.endswith("USDT"):
        raise InvalidSymbolError(
            f"Símbolo {symbol!r} no termina en USDT; _to_ccxt_symbol no sabe partirlo."
        )
    return f"{symbol[:-4]}/USDT"


def get_exchange() -> ccxt.binance:
    """Cliente ccxt contra Binance mainnet, sin credenciales.

    Sin `apiKey`/`secret` a propósito: esta función solo se usa para
    datos públicos de mercado (velas). Un cliente autenticado para
    cuenta/órdenes es responsabilidad de la capa `execution`, no de este
    módulo (ver sección 15.2 del plan: `data` no ejecuta órdenes).
    """
    return ccxt.binance({"enableRateLimit": True})


def fetch_candles(
    symbol: str,
    timeframe: str,
    limit: int = 100,
    since: datetime | None = None,
    exchange: ccxt.binance | None = None,
) -> list[Candle]:
    """Descarga hasta `limit` velas de `symbol` en `timeframe` desde Binance mainnet.

    `since`, si se pasa, debe ser un `datetime` con tzinfo (UTC recomendado);
    de lo contrario se interpretaría con ambigüedad de zona horaria, lo cual
    contradice la sección 15.3 del plan (timestamps en UTC explícito).

    `exchange`, si se pasa, se reutiliza en vez de crear un cliente ccxt
    nuevo. Importa para un backfill que hace muchas llamadas paginadas
    seguidas: el rate-limiting interno de ccxt (`enableRateLimit`) lleva la
    cuenta por instancia, así que crear una instancia nueva en cada llamada
    lo dejaría sin efecto y arriesgaría un 429 de Binance a mitad de un
    backfill grande.
    """
    _validate_symbol(symbol)
    _validate_timeframe(timeframe)
    if limit < 1 or limit > _MAX_LIMIT_PER_CALL:
        raise ValueError(f"limit debe estar entre 1 y {_MAX_LIMIT_PER_CALL}, recibido {limit}")
    if since is not None and since.tzinfo is None:
        raise ValueError("`since` debe ser un datetime con tzinfo (UTC), no naive")

    since_ms = int(since.timestamp() * 1000) if since is not None else None

    exchange = exchange if exchange is not None else get_exchange()
    raw = exchange.fetch_ohlcv(
        _to_ccxt_symbol(symbol),
        timeframe=timeframe,
        limit=limit,
        since=since_ms,
    )

    candles: list[Candle] = []
    for open_time_ms, open_, high, low, close, volume in raw:
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc),
                # str() antes de Decimal: ccxt devuelve float. Pasar el float
                # directo a Decimal arrastraría su imprecisión binaria; pasar
                # por str usa la representación decimal más corta que
                # redondea de vuelta al mismo float (comportamiento estándar
                # de Python), que es lo mejor que se puede hacer partiendo de
                # un float — la próxima limpieza real es que ccxt entregara
                # los valores ya como string/Decimal, cosa que no controlamos.
                open=Decimal(str(open_)),
                high=Decimal(str(high)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(volume)),
            )
        )
    return candles
