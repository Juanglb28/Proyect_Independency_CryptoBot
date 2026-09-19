"""Whitelist explícita de símbolos operables (sección 21 del plan).

"La lista de símbolos permitidos debe ser una lista explícita y controlada.
Nunca debe permitir que un LLM, una noticia, una API externa o un usuario
desde Streamlit introduzca dinámicamente un símbolo negociable fuera de la
lista autorizada."

Por eso esta lista vive aquí, como constante de código versionada en git —
no en `.env` ni en ninguna tabla editable en caliente. Cambiarla exige un
commit explícito y revisable, no una variable de entorno.

Selección (confirmada con el usuario el 2026-09-19): las 20 principales
criptomonedas por capitalización de mercado que tienen un par spot activo
contra USDT en Binance, excluyendo:

- Stablecoins (USDT, USDC, USDS, DAI, USDE): son la moneda de cotización,
  no un prospecto de inversión.
- Tokens nativos de exchanges distintos de Binance u obscuros sin par
  confirmado en Binance (p. ej. WhiteBIT Coin, LEO Token, Figure Heloc,
  Rain).
- Monero (XMR): deslistado de Binance en febrero de 2024 por presión
  regulatoria sobre monedas de privacidad; sigue deslistado.

Ranking de referencia: CoinGecko (consultado 2026-09-19). Confirmación de
pares USDT activos en Binance: binance.com/en/trade/<SYMBOL>_USDT.

Esta lista fija SOLO qué símbolos puede tocar el sistema. No implica que
todos entren a cada ciclo de análisis (eso lo decide el prefiltro, sección
8 del plan) ni que todos tengan la misma liquidez o comportamiento.
"""

from __future__ import annotations

ALLOWED_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "SOLUSDT",
    "TRXUSDT",
    "ZECUSDT",
    "DOGEUSDT",
    "HYPEUSDT",
    "LINKUSDT",
    "ADAUSDT",
    "XLMUSDT",
    "UNIUSDT",
    "BCHUSDT",
    "NEARUSDT",
    "LTCUSDT",
    "AVAXUSDT",
    "HBARUSDT",
    "SHIBUSDT",
    "AAVEUSDT",
)

_EXPECTED_COUNT = 20
assert len(ALLOWED_SYMBOLS) == _EXPECTED_COUNT, (
    f"Se esperaban {_EXPECTED_COUNT} símbolos en la whitelist, hay {len(ALLOWED_SYMBOLS)}"
)
assert len(set(ALLOWED_SYMBOLS)) == len(ALLOWED_SYMBOLS), "Hay símbolos duplicados en la whitelist"
