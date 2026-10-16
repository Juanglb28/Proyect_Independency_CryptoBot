"""Adaptador entre nuestro contexto y el código de Kronos (terceros).

`PredictorDeTrayectorias` es el contrato: recibe contextos YA validados (sin
look-ahead, sin huecos) y devuelve, por contexto, el cierre final de cada
trayectoria pronosticada. Los tests usan un predictor falso; el real
(`KronosTorchPredictor`) importa torch y el código de Kronos SOLO al
construirse, de modo que el resto del proyecto (y su imagen Docker) no los
necesita.

Por qué N trayectorias separadas y no `sample_count`: `KronosPredictor.predict`
devuelve únicamente el PROMEDIO de sus `sample_count` trayectorias (se promedia
dentro de `auto_regressive_inference`), y así no hay dispersión ni fracción al
alza. Para conservarlas se duplica cada contexto N veces en un lote con
`sample_count=1`: el costo de cómputo es el mismo y cada trayectoria queda
disponible. El código de Kronos NO se modifica.

Reproducibilidad: el muestreo usa el generador global de torch, y un lote con
varios contextos comparte ese generador: el resultado de un contexto cambiaría
según sus acompañantes (se comprobó en K0). Por eso se procesa UN contexto por
llamada (sus N trayectorias en un lote) con una semilla derivada de
(semilla base, símbolo, as_of) — `semilla_de_contexto` — y el pronóstico de cada
(símbolo, as_of) es independiente de los demás y del orden. Entre dispositivos o
versiones pueden aparecer diferencias numéricas pequeñas (se registran el
dispositivo y las versiones, no se promete igualdad bit a bit).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.domain.rules.kronos_signal import ParametrosKronos, semilla_de_contexto, validar_parametros
from app.experiments.kronos.contexto import UNA_HORA, ContextoKronos
from app.experiments.kronos.pesos import LockPesos, verificar_carpeta


class PredictorDeTrayectorias(Protocol):
    def cierres_finales(
        self, contextos: Sequence[ContextoKronos], params: ParametrosKronos
    ) -> list[list[Decimal]]:
        """Para cada contexto, `params.n_trajectories` cierres finales (uno por trayectoria)."""
        ...


class KronosTorchPredictor:  # pragma: no cover - necesita torch y los pesos reales
    """Predictor real. Se construye UNA vez (cargar pesos es lo más lento)."""

    def __init__(
        self,
        lock: LockPesos,
        *,
        carpeta_modelo: Path,
        carpeta_tokenizador: Path,
        codigo_kronos: Path | None = None,
        device: str = "cpu",
        max_context: int = 512,
    ) -> None:
        # Verificar ANTES de ejecutar nada de lo descargado.
        verificar_carpeta(carpeta_modelo, lock.modelo)
        verificar_carpeta(carpeta_tokenizador, lock.tokenizador)
        origen = codigo_kronos or Path(os.environ.get("KRONOS_SRC", "/opt/kronos"))
        if not (origen / "model" / "kronos.py").is_file():
            raise FileNotFoundError(f"No se encontró el código de Kronos en {origen} (variable KRONOS_SRC).")
        if str(origen) not in sys.path:
            sys.path.insert(0, str(origen))

        import torch  # noqa: PLC0415 - importación diferida a propósito
        from model import Kronos, KronosPredictor, KronosTokenizer  # noqa: PLC0415

        self._torch = torch
        self.device = device
        tokenizador = KronosTokenizer.from_pretrained(str(carpeta_tokenizador))
        modelo = Kronos.from_pretrained(str(carpeta_modelo))
        tokenizador.eval()
        modelo.eval()
        self._predictor = KronosPredictor(modelo, tokenizador, device=device, max_context=max_context)
        self.max_context = max_context
        self.segundos_ultimo_lote = 0.0

    def cierres_finales(
        self, contextos: Sequence[ContextoKronos], params: ParametrosKronos
    ) -> list[list[Decimal]]:
        import pandas as pd  # noqa: PLC0415

        validar_parametros(params, max_context=self.max_context)
        salida: list[list[Decimal]] = []
        inicio = time.perf_counter()
        for ctx in contextos:
            salida.append(self._un_contexto(ctx, params, pd))
        self.segundos_ultimo_lote = time.perf_counter() - inicio
        return salida

    def _un_contexto(self, ctx: ContextoKronos, params: ParametrosKronos, pd) -> list[Decimal]:
        if len(ctx.velas) != params.lookback or ctx.horizon != params.horizon:
            raise ValueError("El contexto no coincide con lookback/horizon de los parámetros.")
        # Solo OHLC (el preentrenamiento de cripto no tuvo volumen): el predictor rellena volume/amount con ceros.
        df = pd.DataFrame(
            {
                "open": [float(v["open"]) for v in ctx.velas],
                "high": [float(v["high"]) for v in ctx.velas],
                "low": [float(v["low"]) for v in ctx.velas],
                "close": [float(v["close"]) for v in ctx.velas],
            }
        )
        if params.usa_volumen:
            df["volume"] = [float(v["volume"]) for v in ctx.velas]
        # Marcas de tiempo sin zona (UTC implícito): así lo espera el código de Kronos (.dt.hour, etc.).
        x_ts = pd.Series([v["open_time"].replace(tzinfo=None) for v in ctx.velas])
        y_ts = pd.Series([t.replace(tzinfo=None) for t in ctx.aperturas_a_pronosticar])
        n = params.n_trajectories

        self._torch.manual_seed(semilla_de_contexto(params.seed, ctx.symbol, ctx.as_of))
        resultados = self._predictor.predict_batch(
            [df] * n,
            [x_ts] * n,
            [y_ts] * n,
            pred_len=params.horizon,
            T=float(params.temperature),
            top_k=0,
            top_p=float(params.top_p),
            sample_count=1,
            verbose=False,
        )
        return [Decimal(str(float(r["close"].iloc[-1]))) for r in resultados]


# Utilidad usada por los tests y por K0 para no repetir el cálculo del instante final.
def instante_del_precio_final(ctx: ContextoKronos):
    """Instante al que corresponde el cierre final pronosticado: `as_of + horizon` horas."""
    return ctx.as_of + UNA_HORA * ctx.horizon
