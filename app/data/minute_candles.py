"""Resolvedor de velas ambiguas con velas de 1 minuto (estudio, 2026-10-17).

Implementa `app.data.exit_simulation.ResolvedorIntravela`: dada una vela de 1h cuyo
desenlace depende del orden intravela, entrega sus 60 velas de 1 minuto. Es una
herramienta de ESTUDIO de solo lectura: las velas de 1 minuto se descargan de Binance
(datos públicos), se validan contra la vela de 1h y se guardan en un archivo de caché
local. No tocan la base de datos, los indicadores ni el motor paper.

Validación (no se inventa ni se rellena nada): las 60 velas deben ser consecutivas y
agregar EXACTAMENTE la vela de 1h (misma apertura, máximo, mínimo y cierre). Si falta
un minuto, o el agregado no coincide, la vela se declara NO RESUELTA y la simulación
usa la regla de 1h de siempre; el informe cuenta cuántas y por qué.

La caché evita volver a descargar: se guarda por símbolo en
`<directorio>/<SIMBOLO>.json`. Los fallos de red no se guardan (se reintentan en la
siguiente corrida).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import ccxt

from app.data.exchange_client import MINUTOS_POR_HORA, Candle, fetch_minute_candles_of_hour, get_exchange
from app.domain.rules.risk import calculate_trailing_stop

logger = logging.getLogger(__name__)

DIRECTORIO_CACHE_POR_DEFECTO = Path("data/cache_velas_1m")
_UN_MINUTO = timedelta(minutes=1)
_REINTENTOS = 3
_GUARDAR_CADA = 200

MOTIVO_INCOMPLETA = "incompleta"
MOTIVO_INCONSISTENTE = "no_coincide_con_la_vela_de_1h"
MOTIVO_SIN_DESCARGAR = "pendiente_de_descargar"
MOTIVO_ERROR_DE_RED = "error_de_red"


def validar_minutos(vela: Candle, minutos: list[Candle]) -> str | None:
    """`None` si las velas de 1 minuto agregan exactamente `vela`; si no, el motivo."""
    if len(minutos) != MINUTOS_POR_HORA:
        return MOTIVO_INCOMPLETA
    for i, m in enumerate(minutos):
        if m["open_time"] != vela["open_time"] + i * _UN_MINUTO:
            return MOTIVO_INCOMPLETA
    if (
        minutos[0]["open"] != vela["open"]
        or minutos[-1]["close"] != vela["close"]
        or max(m["high"] for m in minutos) != vela["high"]
        or min(m["low"] for m in minutos) != vela["low"]
    ):
        return MOTIVO_INCONSISTENTE
    return None


def minuto_puede_ser_ambiguo(minuto: Candle) -> bool:
    """Condición NECESARIA para que un minuto sea ambiguo por sí solo (cualquier clase):
    su mínimo llega al trailing de su propio máximo, es decir, rango >= 6 % del máximo.
    Si un minuto no la cumple, el orden dentro del minuto no puede cambiar nada."""
    return minuto["low"] <= calculate_trailing_stop(minuto["high"])


def _a_json(minutos: list[Candle]) -> list[list[str]]:
    return [
        [str(int(m["open_time"].timestamp() * 1000)), str(m["open"]), str(m["high"]), str(m["low"]), str(m["close"]), str(m["volume"])]
        for m in minutos
    ]


def _de_json(symbol: str, filas: list[list[str]]) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timeframe="1m",
            open_time=datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc),
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(low),
            close=Decimal(c),
            volume=Decimal(v),
        )
        for ms, o, h, low, c, v in filas
    ]


class ResolvedorMinutos:
    """Callable `(vela_1h, clase) -> list[Candle] | None` con caché en disco.

    `descargar=False` solo cuenta: usa lo que ya esté en la caché y no toca la red
    (las velas no cacheadas quedan `pendiente_de_descargar`)."""

    def __init__(
        self,
        *,
        directorio: Path = DIRECTORIO_CACHE_POR_DEFECTO,
        descargar: bool = True,
        exchange: object | None = None,
        pausa_reintento: float = 1.0,
    ) -> None:
        self._directorio = Path(directorio)
        self._descargar = descargar
        self._exchange = exchange
        self._pausa = pausa_reintento
        self._cache: dict[str, dict[str, list[list[str]]]] = {}
        self._sucia: set[str] = set()
        self._desde_ultimo_guardado = 0
        # Estadísticas para el informe.
        self.consultadas: set[tuple[str, datetime, str]] = set()  # (símbolo, hora, clase), sin repetir
        self.descargadas = 0
        self._bajadas: set[tuple[str, str]] = set()
        self._de_cache: set[tuple[str, str]] = set()  # horas servidas desde la caché de archivos (únicas)
        self.no_resueltas: dict[tuple[str, datetime], str] = {}  # (símbolo, hora) -> motivo
        self.max_rango_minuto = Decimal(0)  # mayor (máx-mín)/mín en un solo minuto usado
        # Minutos usados que, por su rango, podrían ser ambiguos por sí solos: (símbolo, minuto) -> rango.
        self.minutos_ambiguos: dict[tuple[str, datetime], Decimal] = {}

    @property
    def desde_cache(self) -> int:
        return len(self._de_cache)

    # -- caché ------------------------------------------------------------

    def _archivo(self, symbol: str) -> Path:
        return self._directorio / f"{symbol}.json"

    def _cargar(self, symbol: str) -> dict[str, list[list[str]]]:
        if symbol not in self._cache:
            archivo = self._archivo(symbol)
            datos: dict[str, list[list[str]]] = {}
            if archivo.exists():
                try:
                    datos = json.loads(archivo.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    logger.warning("Caché de velas de 1 minuto ilegible (%s); se ignora", archivo)
            self._cache[symbol] = datos
        return self._cache[symbol]

    def guardar(self) -> None:
        """Escribe a disco las cachés modificadas (atómico: archivo temporal + renombrar)."""
        if not self._sucia:
            return
        self._directorio.mkdir(parents=True, exist_ok=True)
        for symbol in sorted(self._sucia):
            destino = self._archivo(symbol)
            temporal = destino.with_suffix(".json.tmp")
            temporal.write_text(json.dumps(self._cache[symbol], separators=(",", ":")), encoding="utf-8")
            os.replace(temporal, destino)
        self._sucia.clear()
        self._desde_ultimo_guardado = 0

    # -- descarga ---------------------------------------------------------

    def _bajar(self, symbol: str, hora: datetime) -> list[Candle] | None:
        if self._exchange is None:
            self._exchange = get_exchange()
        for intento in range(1, _REINTENTOS + 1):
            try:
                return fetch_minute_candles_of_hour(symbol, hora, self._exchange)
            except ccxt.NetworkError:
                if intento == _REINTENTOS:
                    logger.error("Binance no respondió para %s %s tras %d intentos", symbol, hora, _REINTENTOS)
                    return None
                time.sleep(self._pausa * intento)
        return None

    # -- interfaz ResolvedorIntravela -----------------------------------------

    def __call__(self, vela: Candle, clase: str) -> list[Candle] | None:
        symbol, hora = vela["symbol"], vela["open_time"]
        self.consultadas.add((symbol, hora, clase))
        clave = hora.isoformat()
        cache = self._cargar(symbol)

        minutos: list[Candle] | None
        if clave in cache:
            minutos = _de_json(symbol, cache[clave])
            if (symbol, clave) not in self._bajadas:
                self._de_cache.add((symbol, clave))
        elif not self._descargar:
            self.no_resueltas[(symbol, hora)] = MOTIVO_SIN_DESCARGAR
            return None
        else:
            minutos = self._bajar(symbol, hora)
            if minutos is None:
                self.no_resueltas[(symbol, hora)] = MOTIVO_ERROR_DE_RED
                return None
            self.descargadas += 1
            self._bajadas.add((symbol, clave))
            cache[clave] = _a_json(minutos)
            self._sucia.add(symbol)
            self._desde_ultimo_guardado += 1
            if self._desde_ultimo_guardado >= _GUARDAR_CADA:
                self.guardar()

        motivo = validar_minutos(vela, minutos)
        if motivo is not None:
            self.no_resueltas[(symbol, hora)] = motivo
            return None
        self.no_resueltas.pop((symbol, hora), None)
        for m in minutos:
            if m["low"] > 0:
                rango = (m["high"] - m["low"]) / m["low"]
                self.max_rango_minuto = max(self.max_rango_minuto, rango)
                if minuto_puede_ser_ambiguo(m):
                    self.minutos_ambiguos[(symbol, m["open_time"])] = rango
        return minutos

    # -- resumen ----------------------------------------------------------

    def velas_ambiguas_unicas(self) -> set[tuple[str, datetime]]:
        return {(s, h) for s, h, _ in self.consultadas}

    def por_clase(self) -> dict[str, int]:
        cuenta: dict[str, int] = {}
        for _, _, clase in self.consultadas:
            cuenta[clase] = cuenta.get(clase, 0) + 1
        return dict(sorted(cuenta.items()))

    def motivos_no_resueltas(self) -> dict[str, int]:
        cuenta: dict[str, int] = {}
        for motivo in self.no_resueltas.values():
            cuenta[motivo] = cuenta.get(motivo, 0) + 1
        return dict(sorted(cuenta.items()))
