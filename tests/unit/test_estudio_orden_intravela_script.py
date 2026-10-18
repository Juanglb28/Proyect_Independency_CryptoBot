"""Script `scripts.estudio_orden_intravela`: argumentos, formato y que la lectura por defecto no cambia."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.backtest_study import observaciones_de_simbolo
from app.data.exit_simulation import MAXIMO_PRIMERO, MINIMO_PRIMERO
from app.domain.rules.intravela import comparar_ordenes
from scripts import estudio_orden_intravela as script

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _serie(dias: int, semilla: int):
    """Caminata aleatoria sintética: velas de 1h y su agregado diario (sin base de datos)."""
    rng = random.Random(semilla)
    precio = Decimal(100)
    horarias = []
    for i in range(dias * 24):
        abre = precio
        cierra = max(Decimal("1"), abre * (1 + Decimal(rng.gauss(0, 0.006)).quantize(Decimal("0.00001"))))
        alto = max(abre, cierra) * (1 + Decimal(abs(rng.gauss(0, 0.004))).quantize(Decimal("0.00001")))
        bajo = min(abre, cierra) * (1 - Decimal(abs(rng.gauss(0, 0.004))).quantize(Decimal("0.00001")))
        horarias.append(dict(symbol="X", timeframe="1h", open_time=T0 + timedelta(hours=i), open=abre, high=alto,
                             low=bajo, close=cierra, volume=Decimal(rng.randint(50, 150))))
        precio = cierra
    diarias = []
    for k in range(0, len(horarias), 24):
        g = horarias[k : k + 24]
        diarias.append(dict(symbol="X", timeframe="1d", open_time=g[0]["open_time"], open=g[0]["open"],
                            high=max(x["high"] for x in g), low=min(x["low"] for x in g), close=g[-1]["close"],
                            volume=sum(x["volume"] for x in g)))
    return diarias, horarias


def test_un_simbolo_fuera_de_la_lista_autorizada_se_rechaza_sin_tocar_la_base(capsys):
    assert script.main(["--symbols", "DOGEFAKEUSDT"]) == 1
    assert "no está en la lista autorizada" in capsys.readouterr().err


def test_la_lectura_por_defecto_es_identica_a_no_pasar_el_parametro():
    diarias, horarias = _serie(260, 11)
    sin_parametro = observaciones_de_simbolo("X", diarias, horarias)
    explicita = observaciones_de_simbolo("X", diarias, horarias, orden_intravela=MAXIMO_PRIMERO)
    assert sin_parametro == explicita and sin_parametro


def test_las_dos_lecturas_comparten_score_y_el_informe_se_imprime(capsys):
    diarias, horarias = _serie(300, 5)
    base = observaciones_de_simbolo("X", diarias, horarias, orden_intravela=MAXIMO_PRIMERO)
    inversa = observaciones_de_simbolo("X", diarias, horarias, orden_intravela=MINIMO_PRIMERO)
    c = comparar_ordenes(base, inversa)  # lanza si el score o las claves difieren
    assert c.n_total == len(base) == len(inversa)
    script.imprimir(c, base, inversa)
    salida = capsys.readouterr().out
    for esperado in (
        "SENSIBILIDAD AL ORDEN INTRAVELA", "Resueltas en AMBAS lecturas", "Diferencia media", "Entradas que cambian",
        "discriminación del score", "Veredicto walk-forward", "ninguna de las dos lecturas es",
    ):
        assert esperado in salida
