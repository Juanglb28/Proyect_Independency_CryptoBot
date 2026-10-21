"""Script `scripts.estudio_friccion` (prueba de estrés de fricción, 2026-10-21): tabla fija de
escenarios, punto de equilibrio, formato del informe y bucle de escenarios (sin base de datos)."""

from __future__ import annotations

import random
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.data.exit_simulation import SIN_FRICCION, Friccion
from app.domain.rules.walk_forward import ObservacionBacktest, informe_walk_forward
from scripts import estudio_friccion as script

D = Decimal


def test_la_tabla_de_escenarios_es_la_del_protocolo():
    """Pinea `claude/protocolo-friccion.md`: cambiarla exige un protocolo nuevo, no un retoque."""
    esperado = {
        "E0": (D(0), D(0), False),
        "G": (D(0), D(0), True),
        "S10": (D("0.0010"), D(0), False),
        "S25": (D("0.0025"), D(0), False),
        "S50": (D("0.0050"), D(0), False),
        "S10G": (D("0.0010"), D(0), True),
        "S25G": (D("0.0025"), D(0), True),
        "S50G": (D("0.0050"), D(0), True),
        "X": (D("0.0010"), D("0.0050"), True),
    }
    actual = {e.id: (e.friccion.slippage_por_lado, e.friccion.extra_slippage_stop, e.friccion.huecos) for e in script.ESCENARIOS}
    assert actual == esperado
    assert [e.id for e in script.ESCENARIOS][0] == "E0"
    assert script.ESCENARIOS[0].friccion == SIN_FRICCION


def test_punto_de_equilibrio_interpola_el_primer_cruce():
    puntos = [(D("0"), D("0.010")), (D("0.001"), D("0.006")), (D("0.0025"), D("0.002")), (D("0.005"), D("-0.002"))]
    estado, valor = script.punto_de_equilibrio(puntos)
    assert estado == "CRUZA"
    # entre 0,25 % (+0,002) y 0,50 % (-0,002): cruza justo a mitad = 0,375 %
    assert valor == D("0.00375")


def test_punto_de_equilibrio_casos_sin_cruce():
    assert script.punto_de_equilibrio([(D(0), D("0.01")), (D("0.005"), D("0.004"))]) == ("NO_CRUZA", None)
    assert script.punto_de_equilibrio([(D(0), D("-0.01")), (D("0.005"), D("-0.02"))]) == ("YA_NEGATIVA", None)
    assert script.punto_de_equilibrio([(D(0), D(0)), (D("0.005"), D("-0.02"))]) == ("YA_NEGATIVA", None)
    with pytest.raises(ValueError):
        script.punto_de_equilibrio([])


def _observaciones(desplazamiento: Decimal = D(0), semilla: int = 3) -> list[ObservacionBacktest]:
    rng = random.Random(semilla)
    inicio = date(2025, 1, 1)
    return [
        ObservacionBacktest(
            f"S{s}", inicio + timedelta(days=d), D(rng.randint(0, 100)), D(rng.randint(-40, 60)) / 1000 + desplazamiento,
            datetime.combine(inicio + timedelta(days=d + 2), datetime.min.time(), tzinfo=timezone.utc),
        )
        for d in range(520) for s in range(3)
    ]


def test_la_fila_muestra_alto_bajo_diferencia_y_veredicto():
    informe = informe_walk_forward(_observaciones())
    texto = script.fila(script.ESCENARIOS[2], informe)
    for esperado in ("S10", "alto=", "bajo=", "dif=", "IC95=", informe.veredicto.value, "pruebas+="):
        assert esperado in texto


def test_el_resumen_dice_si_el_veredicto_cambia_y_da_los_equilibrios():
    informes = {
        e.id: informe_walk_forward(_observaciones(desplazamiento=D(0) - e.friccion.slippage_por_lado * 4))
        for e in script.ESCENARIOS
    }
    lineas = script.resumen(informes)
    assert any(l.startswith("Veredicto vs. E0") for l in lineas)
    assert sum("Equilibrio del tercil alto" in l for l in lineas) == 2  # sin huecos y con huecos


def test_el_resumen_funciona_con_un_subconjunto_de_escenarios():
    informe = informe_walk_forward(_observaciones())
    lineas = script.resumen({"E0": informe, "S25": informe})
    assert lineas == ["Veredicto vs. E0 (" + informe.veredicto.value + "): no cambia en ningún escenario corrido"]


def test_simbolo_fuera_de_la_lista_autorizada_se_rechaza_sin_tocar_la_base(capsys):
    assert script.main(["--symbols", "DOGEFAKEUSDT"]) == 1
    assert "no está en la lista autorizada" in capsys.readouterr().err


def test_escenario_desconocido_se_rechaza_sin_tocar_la_base(capsys):
    assert script.main(["--escenarios", "E0,NOPE"]) == 1
    assert "desconocido" in capsys.readouterr().err


def test_hasta_fecha_exige_una_fecha_valida():
    with pytest.raises(SystemExit):
        script.main(["--hasta-fecha", "no-es-fecha"])


def _correr_main(argv):
    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    llamadas: list[tuple[str, Friccion]] = []

    def observaciones(symbol, diarias, horarias, *, friccion):
        llamadas.append((symbol, friccion))
        return _observaciones(semilla=len(llamadas))

    with (
        patch.object(script, "configure_logging"),
        patch.object(script, "get_settings"),
        patch.object(script, "get_engine"),
        patch.object(script, "get_sessionmaker"),
        patch.object(script, "session_scope", scope),
        patch.object(script, "velas_vencidas_que_invalidan_un_estudio", return_value=[]),
        patch.object(script, "get_candles", return_value=[]),
        patch.object(script, "observaciones_de_simbolo", side_effect=observaciones),
    ):
        codigo = script.main(argv)
    return codigo, llamadas


def test_main_corre_cada_escenario_por_cada_simbolo_con_su_friccion(capsys):
    codigo, llamadas = _correr_main(["--symbols", "BTCUSDT,ETHUSDT"])
    assert codigo == 0
    assert len(llamadas) == 2 * len(script.ESCENARIOS)
    fricciones = {f for _, f in llamadas}
    assert fricciones == {e.friccion for e in script.ESCENARIOS}
    salida = capsys.readouterr().out
    assert "protocolo-friccion.md" in salida and "CONTROL" in salida and "Cautelas" in salida
    for e in script.ESCENARIOS:
        assert e.id in salida


def test_main_con_subconjunto_solo_corre_esos_escenarios(capsys):
    codigo, llamadas = _correr_main(["--symbols", "BTCUSDT", "--escenarios", "E0,S25G"])
    assert codigo == 0
    assert [f for _, f in llamadas] == [SIN_FRICCION, Friccion(D("0.0025"), D(0), True)]
    assert "S25G" in capsys.readouterr().out


def test_main_con_hasta_fecha_filtra_y_lo_dice(capsys):
    codigo, _ = _correr_main(["--symbols", "BTCUSDT", "--escenarios", "E0", "--hasta-fecha", "2026-01-01"])
    assert codigo == 0
    salida = capsys.readouterr().out
    assert "anteriores a 2026-01-01" in salida and "--hasta-fecha 2026-01-01" in salida


def test_main_se_detiene_si_hay_velas_vencidas_sin_cerrar(capsys):
    @contextmanager
    def scope(_fabrica):
        yield MagicMock()

    with (
        patch.object(script, "configure_logging"),
        patch.object(script, "get_settings"),
        patch.object(script, "get_engine"),
        patch.object(script, "get_sessionmaker"),
        patch.object(script, "session_scope", scope),
        patch.object(script, "velas_vencidas_que_invalidan_un_estudio", return_value=[object()]),
        patch.object(script, "observaciones_de_simbolo") as obs,
    ):
        assert script.main(["--symbols", "BTCUSDT"]) == 1
    obs.assert_not_called()
    assert "vencidas sin cerrar" in capsys.readouterr().err
