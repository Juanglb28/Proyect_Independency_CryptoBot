"""Comparación de corridas de K0: estabilidad del ranking entre semillas (E6)."""

from __future__ import annotations

import json
from decimal import Decimal

from app.experiments.kronos.comparar import comparar_corridas, formatear_comparacion

D = Decimal
AS_OF = "2025-03-01T00:00:00+00:00"


def _corrida(valores, n=8, disp="0.02"):
    return {
        "pronosticos": [
            {
                "symbol": f"S{i}USDT", "as_of": AS_OF, "retorno_realizado": None,
                "resumen": {"n": n, "expected_return": str(v), "prob_up": "0.5", "dispersion": disp},
            }
            for i, v in enumerate(valores)
        ]
    }


def test_corridas_identicas_correlacion_uno_y_mismo_signo_total():
    c = comparar_corridas(_corrida([0.01, -0.02, 0.03, 0.0, -0.01]), _corrida([0.01, -0.02, 0.03, 0.0, -0.01]))
    assert c.pares == 5 and c.solo_en_una == 0
    assert c.spearman_retorno_esperado == D("1")
    assert c.diferencia_absoluta_media == 0


def test_ranking_invertido_da_menos_uno():
    c = comparar_corridas(_corrida([1, 2, 3, 4, 5]), _corrida([5, 4, 3, 2, 1]))
    assert c.spearman_retorno_esperado == D("-1")


def test_error_estandar_es_dispersion_sobre_raiz_de_n():
    c = comparar_corridas(_corrida([0.01, 0.02, 0.03], n=4, disp="0.04"), _corrida([0.01, 0.02, 0.03], n=4, disp="0.04"))
    assert c.error_estandar_medio == D("0.02")


def test_solo_se_comparan_los_pronosticos_presentes_en_ambas():
    a, b = _corrida([1, 2, 3, 4]), _corrida([1, 2, 3])
    c = comparar_corridas(a, b)
    assert c.pares == 3 and c.solo_en_una == 1


def test_sin_pares_no_inventa_cifras():
    c = comparar_corridas({"pronosticos": []}, {"pronosticos": []})
    assert c.pares == 0 and c.spearman_retorno_esperado is None and c.error_estandar_medio is None


def test_el_informe_no_menciona_retorno_realizado_como_resultado():
    texto = "\n".join(formatear_comparacion(comparar_corridas(_corrida([1, 2, 3]), _corrida([3, 2, 1]))))
    assert "no capacidad predictiva" in texto and "FIABILIDAD" in texto


def test_el_script_lee_dos_json(tmp_path, capsys):
    from scripts.kronos_k0_comparar import main

    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    pa.write_text(json.dumps(_corrida([1, 2, 3, 4])))
    pb.write_text(json.dumps(_corrida([1, 2, 4, 3])))
    assert main([str(pa), str(pb)]) == 0
    assert "Pronósticos comparables: 4" in capsys.readouterr().out
    assert main([str(pa)]) == 2
