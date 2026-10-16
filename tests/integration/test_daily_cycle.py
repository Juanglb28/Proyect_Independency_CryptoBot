"""Tests de app.orchestration.daily_cycle.run_daily_cycle (Paso 7) contra
Postgres real de pruebas.

Igual que tests/integration/test_consolidator_agent.py, se parchea
`call_agent` (nunca `run_json_agent` ni capas más internas) para no
depender de la API real de Anthropic — pero el pre-filtro (parcheado
aparte, para no depender de que los 20 símbolos de la whitelist tengan
datos suficientes en la base de pruebas), las lecturas de datos por
candidato, la validación de schema, el cálculo del score y la persistencia
SÍ corren de verdad.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.ai.client import AgentCallResult
from app.config import Settings
from app.data.exchange_client import Candle
from app.data.indicator_calculator import IndicatorRow, sma_indicator_name
from app.data.prefilter import PrefilterScore
from app.data.prefilter_pipeline import PrefilterRunResult
from app.domain.enums import EstadoCiclo, TierEntrada
from app.domain.rules.data_quality import (
    DATA_QUALITY_FORMULA_VERSION,
    DataQualityResult,
)
from app.domain.rules.quantitative_score import QUANTITATIVE_FORMULA_VERSION
from app.orchestration.daily_cycle import run_daily_cycle
from app.repositories.analysis import (
    PropuestaRow,
    create_ciclo,
    finalizar_ciclo,
    get_ciclo,
    get_factores_de_propuesta,
    get_propuestas_de_ciclo,
    save_propuesta,
)
from app.repositories.candles import save_candles
from app.repositories.indicators import save_indicators

_OPEN_TIME = datetime(2026, 9, 29, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(database_url="postgresql+psycopg://x:x@localhost/x", anthropic_api_key="sk-test")


def _candle(symbol: str) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe="1d",
        open_time=_OPEN_TIME,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("95"),
        close=Decimal("105"),
        volume=Decimal("1000"),
    )


def _indicators(symbol: str) -> list[IndicatorRow]:
    rows = [
        IndicatorRow(symbol, "1d", _OPEN_TIME, "RSI_14", Decimal("55")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "ATR_14", Decimal("3.0")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "VOLUMEN_RELATIVO_30", Decimal("1.5")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "DISTANCIA_ATH", Decimal("-10")),
        IndicatorRow(symbol, "1d", _OPEN_TIME, "DISTANCIA_MAX_90D", Decimal("-3")),
    ]
    for period in (90, 120, 180):
        rows.append(IndicatorRow(symbol, "1d", _OPEN_TIME, sma_indicator_name(period), Decimal("100")))
    return rows


def _seed_symbol(session, symbol: str) -> None:
    save_candles(session, [_candle(symbol)])
    save_indicators(session, _indicators(symbol))


def _perfect_data_quality(**kwargs) -> DataQualityResult:
    """DataQualityResult con calidad = 100 (sin penalizaciones, sin bloqueantes).

    Los tests de integración parchean `calculate_data_quality` con esta
    función para aislar la orquestación de la fórmula de calidad — los
    tests unitarios de `test_data_quality.py` ya cubren la fórmula en sí.
    """
    return DataQualityResult(
        calidad_datos=Decimal(100),
        penalizacion_campos=Decimal(0),
        penalizacion_antiguedad=Decimal(0),
        penalizacion_huecos=Decimal(0),
        anomalias=(),
        presenta_anomalias=False,
        bloqueantes=(),
        formula_version=DATA_QUALITY_FORMULA_VERSION,
    )


def _blocked_data_quality(**kwargs) -> DataQualityResult:
    """DataQualityResult con calidad = 0 y un bloqueante (para tests de
    exclusión por calidad)."""
    return DataQualityResult(
        calidad_datos=Decimal(0),
        penalizacion_campos=Decimal(0),
        penalizacion_antiguedad=Decimal(0),
        penalizacion_huecos=Decimal(0),
        anomalias=(),
        presenta_anomalias=False,
        bloqueantes=("sin_velas_diarias: bloqueante de test",),
        formula_version=DATA_QUALITY_FORMULA_VERSION,
    )


_DQ_PATCH_TARGET = "app.orchestration.daily_cycle.calculate_data_quality"


def _fake_prefilter(symbols: list[str]):
    def fake(session):
        candidates = tuple(
            PrefilterScore(
                symbol=symbol,
                calculated_at=_OPEN_TIME,
                tendencia_pct=Decimal("5"),
                volumen_relativo=Decimal("1.5"),
                distancia_max_90d_pct=Decimal("-3"),
                score_tendencia=Decimal("60"),
                score_volumen=Decimal("60"),
                score_distancia=Decimal("60"),
                score_final=Decimal("60"),
            )
            for symbol in symbols
        )
        return PrefilterRunResult(scores=candidates, candidates=candidates, symbols_sin_datos=())

    return fake


def _make_fake_call_agent(calidad_por_simbolo: dict[str, str], puntajes: dict[str, tuple[int, int, int, int]] | None = None):
    """`calidad_por_simbolo[symbol]` controla la `calidad_global` que el
    Auditor "reporta" para ese símbolo — el resto de la respuesta de cada
    agente es fija, suficiente para pasar los 4 schemas."""

    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        datos = json.loads(user_content)

        if "analista cuantitativo" in system_prompt:
            resp = {
                "simbolo": datos["simbolo"],
                "observaciones": [],
                "datos_faltantes": [],
                "conflictos_entre_temporalidades": "ninguno detectado",
                "confianza": "alta",
                "justificacion_confianza": "datos completos para este test",
            }
        elif "analista de contexto macro" in system_prompt:
            resp = {
                "simbolo": datos["simbolo"],
                "eventos_comparables": [],
                "tamano_muestra": 0,
                "consistencia_historica": "mixta",
                "lectura_general": "sin eventos comparables en este test",
                "confianza": "media",
                "anomalias_detectadas": [],
            }
        elif "auditor de calidad de datos" in system_prompt:
            calidad = calidad_por_simbolo.get(datos["simbolo"], "suficiente")
            resp = {
                "campos_faltantes": [],
                "huecos_en_series": [],
                "valores_anomalos": [],
                "antiguedad_maxima_horas": 5.0,
                "calidad_global": calidad,
                "penalizacion_confianza_pct": 50.0 if calidad == "insuficiente" else 0.0,
                "bloqueantes": ["datos insuficientes para este test"] if calidad == "insuficiente" else [],
            }
        elif "consolidador de análisis" in system_prompt:
            candidatos = datos["candidatos"]
            def _p(simbolo):
                # (tendencia_momentum, volumen_liquidez, posicion_rangos_ath, contexto_eventos)
                return (puntajes or {}).get(simbolo, (70, 60, 50, 80))

            opciones = [
                {
                    "simbolo": c["simbolo"],
                    "categoria": "principal",
                    "horizonte": "corto",
                    "nivel_riesgo": "medio",
                    "tendencia_momentum": _p(c["simbolo"])[0],
                    "volumen_liquidez": _p(c["simbolo"])[1],
                    "posicion_rangos_ath": _p(c["simbolo"])[2],
                    "contexto_eventos": _p(c["simbolo"])[3],
                    "tesis": "tesis de prueba",
                    "invalidacion": "invalidación de prueba",
                    "escenario_adverso": {
                        "descripcion": "reversión de prueba",
                        "desencadenante": "gatillo de prueba",
                        "probabilidad_relativa": "baja",
                    },
                    "comentario_chartista": None,
                    "fuentes": [],
                }
                for c in candidatos
            ]
            resp = {
                "recomendacion": "invertir" if opciones else "no_invertir",
                "razon_no_invertir": None if opciones else "sin candidatos que sostengan una tesis",
                "opciones": opciones,
                "confianza_global": "alta",
            }
        else:
            raise AssertionError(f"system_prompt no reconocido en el fake: {system_prompt[:80]!r}")

        return AgentCallResult(raw_text=json.dumps(resp), input_tokens=100, output_tokens=100)

    return fake_call_agent


def test_sin_candidatos_el_ciclo_termina_sin_ninguna_llamada(monkeypatch, db_session):
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter([]))

    def _fail_si_se_llama(*args, **kwargs):
        raise AssertionError("no debería llamarse a ningún agente sin candidatos")

    monkeypatch.setattr("app.ai.agents._runner.call_agent", _fail_si_se_llama)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.propuesta_ids == ()
    assert result.cost_usd == Decimal(0)
    ciclo = get_ciclo(db_session, result.ciclo_id)
    assert ciclo.status == EstadoCiclo.PREFILTER
    assert ciclo.finished_at is not None
    assert ciclo.error_message is None


def test_ciclo_completo_con_dos_candidatos_persiste_propuestas_y_factores(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente", "ETHUSDT": "suficiente"}),
    )
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert set(result.candidatos_evaluados) == {"BTCUSDT", "ETHUSDT"}
    assert result.candidatos_excluidos_por_calidad == ()
    assert len(result.propuesta_ids) == 2
    assert result.cost_usd > 0

    ciclo = get_ciclo(db_session, result.ciclo_id)
    assert ciclo.status == EstadoCiclo.FREEZE_DECISION_SNAPSHOT
    assert ciclo.error_message is None
    assert ciclo.cost_usd == result.cost_usd

    propuestas = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    assert {p.symbol for p in propuestas} == {"BTCUSDT", "ETHUSDT"}
    for propuesta in propuestas:
        assert propuesta.supera_umbral_entrada is True  # 70*.3+60*.2+50*.2+80*.3=67 * calidad 1.0
        assert propuesta.tier_entrada == TierEntrada.NORMAL
        assert propuesta.segunda_ronda is None  # fuera de la zona [50, 55): nunca se evalúa
        assert propuesta.ciclos_consecutivos_en_zona == 0  # 67 no está en la zona
        assert propuesta.decision_snapshot["symbol"] == propuesta.symbol
        assert propuesta.decision_snapshot["analysis_cycle_id"] == str(result.ciclo_id)
        assert "initial_stop" not in propuesta.decision_snapshot
        assert "initial_target" not in propuesta.decision_snapshot

        factores = get_factores_de_propuesta(db_session, propuesta.id)
        assert len(factores) == 4
        assert {f.factor for f in factores} == {
            "tendencia_momentum",
            "volumen_liquidez",
            "posicion_rangos_ath",
            "contexto_eventos",
        }
        assert all(f.confianza == "alta" for f in factores)  # confianza_global del Consolidador


def test_candidato_con_calidad_insuficiente_se_excluye_del_consolidador(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    _seed_symbol(db_session, "ETHUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT", "ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente", "ETHUSDT": "suficiente"}),
    )

    # BTCUSDT bloqueado por calidad determinista; ETHUSDT perfecto.
    _call_count = {"n": 0}

    def _dq_btc_bloqueado(**kwargs):
        _call_count["n"] += 1
        if _call_count["n"] == 1:  # primer candidato: BTCUSDT
            return _blocked_data_quality(**kwargs)
        return _perfect_data_quality(**kwargs)

    monkeypatch.setattr(_DQ_PATCH_TARGET, _dq_btc_bloqueado)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.candidatos_excluidos_por_calidad == ("BTCUSDT",)
    propuestas = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    assert {p.symbol for p in propuestas} == {"ETHUSDT"}


def test_todos_los_candidatos_insuficientes_termina_sin_llamar_al_consolidador(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))

    llamadas_consolidador: list[str] = []

    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        if "consolidador de análisis" in system_prompt:
            llamadas_consolidador.append(system_prompt)
        return _make_fake_call_agent({"BTCUSDT": "suficiente"})(
            settings, model=model, system_prompt=system_prompt, user_content=user_content, max_tokens=max_tokens
        )

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)
    # El bloqueo ahora viene de la fórmula determinista, no del auditor.
    monkeypatch.setattr(_DQ_PATCH_TARGET, _blocked_data_quality)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert llamadas_consolidador == []
    assert result.propuesta_ids == ()
    assert result.candidatos_excluidos_por_calidad == ("BTCUSDT",)

    ciclo = get_ciclo(db_session, result.ciclo_id)
    assert ciclo.status == EstadoCiclo.AI_ANALYSIS
    assert ciclo.error_message is None


def test_consolidador_no_invertir_queda_expuesto_en_el_resultado(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)
    base = _make_fake_call_agent({"BTCUSDT": "suficiente"})

    def fake_call_agent(settings, *, model, system_prompt, user_content, max_tokens=4096):
        if "consolidador de análisis" in system_prompt:
            resp = {
                "recomendacion": "no_invertir",
                "razon_no_invertir": "ningún candidato sostiene una tesis hoy",
                "opciones": [],
                "confianza_global": "media",
            }
            return AgentCallResult(raw_text=json.dumps(resp), input_tokens=100, output_tokens=100)
        return base(settings, model=model, system_prompt=system_prompt, user_content=user_content, max_tokens=max_tokens)

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    assert result.propuesta_ids == ()
    assert result.consolidador_recomendacion == "no_invertir"
    assert result.consolidador_razon_no_invertir == "ningún candidato sostiene una tesis hoy"
    assert result.consolidador_confianza_global == "media"


def test_ciclo_sin_llamar_al_consolidador_deja_su_recomendacion_en_none(monkeypatch, db_session):
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter([]))
    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)
    assert result.consolidador_recomendacion is None
    assert result.consolidador_razon_no_invertir is None


def test_snapshot_contiene_calidad_datos_determinista(monkeypatch, db_session):
    """El decision_snapshot de cada propuesta debe incluir la sección
    `calidad_datos_determinista` con los valores de la fórmula (decisión
    del usuario, 2026-10-08) y `auditoria_datos_ia` como observador."""
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente"}),
    )
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)
    (propuesta,) = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    snap = propuesta.decision_snapshot

    # Sección de calidad determinista
    assert "calidad_datos_determinista" in snap
    cdd = snap["calidad_datos_determinista"]
    assert cdd["calidad_datos"] == "100"
    assert cdd["penalizacion_campos"] == "0"
    assert cdd["penalizacion_antiguedad"] == "0"
    assert cdd["penalizacion_huecos"] == "0"
    assert cdd["presenta_anomalias"] is False
    assert cdd["anomalias"] == []
    assert cdd["formula_version"] == DATA_QUALITY_FORMULA_VERSION

    # Auditor IA como observador (output guardado pero sin efecto en score)
    assert "auditoria_datos_ia" in snap


def test_snapshot_contiene_score_cuantitativo_paralelo(monkeypatch, db_session):
    """El decision_snapshot debe incluir `score_cuantitativo_paralelo` con
    los sub-scores calculados a partir de los indicadores diarios del
    candidato (Fase B — shadow scoring, decisión del usuario 2026-10-08).

    Con los fixtures estándar (_candle close=105, SMA=100, VOLUMEN_RELATIVO=1.5,
    DISTANCIA_MAX_90D=-3, RSI=55):
    - tendencia_pct = 5.0 (promedio de distancia_pct_sma: 5% para cada SMA)
    - score_tendencia = 60.0  (5 en [-25,+25] → (5+25)/50*100)
    - score_volumen = 91.66... (1.5 en [0.4,1.6] → (1.5-0.4)/1.2*100)
    - score_distancia = 85.0  (-3 en [-20,0] → (-3+20)/20*100)
    - score_rsi = 62.5        (55 en [30,70] → (55-30)/40*100)
    - score_final = promedio de los 4
    """
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente"}),
    )
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)
    (propuesta,) = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    snap = propuesta.decision_snapshot

    assert "score_cuantitativo_paralelo" in snap
    sqp = snap["score_cuantitativo_paralelo"]

    # Todos los campos deben estar presentes (no None — datos completos)
    assert sqp["score_final"] is not None
    assert sqp["score_tendencia"] is not None
    assert sqp["score_volumen"] is not None
    assert sqp["score_distancia"] is not None
    assert sqp["score_rsi"] is not None

    # Valores crudos
    assert sqp["tendencia_pct"] is not None
    assert sqp["volumen_relativo"] is not None
    assert sqp["distancia_max_90d_pct"] is not None
    assert sqp["rsi"] is not None
    assert sqp["formula_version"] == QUANTITATIVE_FORMULA_VERSION

    # Verificar valores exactos (serializados como str de Decimal)
    assert sqp["score_tendencia"] == "60.0"
    assert sqp["score_distancia"] == "85.00"
    assert sqp["score_rsi"] == "62.500"
    assert sqp["tendencia_pct"] == "5.0"

    # score_volumen y score_final tienen decimales largos por la división —
    # verificamos con startswith para no anclar una representación exacta
    # de Decimal periódico.
    assert sqp["score_volumen"].startswith("91.6666")
    assert sqp["score_final"].startswith("74.791")


def test_snapshot_con_anomalias_informativas_sin_penalizacion(monkeypatch, db_session):
    """Un candidato con anomalías pero sin bloqueantes debe generar propuesta
    con calidad < 100, y el snapshot debe reflejar las anomalías."""
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"BTCUSDT": "suficiente"}),
    )

    def _dq_con_anomalias(**kwargs):
        return DataQualityResult(
            calidad_datos=Decimal(91),
            penalizacion_campos=Decimal(9),
            penalizacion_antiguedad=Decimal(0),
            penalizacion_huecos=Decimal(0),
            anomalias=("campos_faltantes (3): rsi_14_4h, atr_14_4h, volumen_relativo_30_4h",),
            presenta_anomalias=True,
            bloqueantes=(),
            formula_version=DATA_QUALITY_FORMULA_VERSION,
        )

    monkeypatch.setattr(_DQ_PATCH_TARGET, _dq_con_anomalias)

    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)
    (propuesta,) = get_propuestas_de_ciclo(db_session, result.ciclo_id)

    # La propuesta se generó (no bloqueada)
    assert propuesta.symbol == "BTCUSDT"
    # calidad_datos = 91 → el puntaje ponderado se multiplica por 0.91
    assert propuesta.calidad_datos == Decimal(91)

    snap = propuesta.decision_snapshot
    cdd = snap["calidad_datos_determinista"]
    assert cdd["presenta_anomalias"] is True
    assert len(cdd["anomalias"]) == 1
    assert "campos_faltantes" in cdd["anomalias"][0]


def test_error_en_un_agente_marca_el_ciclo_como_fallido_y_relanza(monkeypatch, db_session):
    _seed_symbol(db_session, "BTCUSDT")
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["BTCUSDT"]))

    def fake_call_agent_roto(settings, *, model, system_prompt, user_content, max_tokens=4096):
        raise RuntimeError("la API de Anthropic no respondió (simulado)")

    monkeypatch.setattr("app.ai.agents._runner.call_agent", fake_call_agent_roto)

    with pytest.raises(RuntimeError, match="no respondió"):
        run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)

    # El ciclo debe quedar registrado como fallido en la ÚLTIMA etapa
    # alcanzada (sección 15.6: nunca saltar en silencio a una etapa
    # posterior) — no debe desaparecer ni quedar en un estado ambiguo.
    from sqlalchemy import select

    from app.db.models.analysis import CicloAnalisis

    ciclo = db_session.scalars(
        select(CicloAnalisis).order_by(CicloAnalisis.created_at.desc()).limit(1)
    ).first()
    assert ciclo is not None
    assert ciclo.status == EstadoCiclo.AI_ANALYSIS
    assert ciclo.error_message is not None
    assert "no respondió" in ciclo.error_message
    assert ciclo.finished_at is not None


# ---------------------------------------------------------------------------
# Segunda ronda — entradas "de mayor riesgo" (decisión del usuario, 2026-10-04)
# ---------------------------------------------------------------------------

# (55*.3)+(40*.2)+(50*.2)+(55*.3) = 51.0 -> dentro de la zona [50, 55)
_PUNTAJES_EN_ZONA = {"ETHUSDT": (55, 40, 50, 55)}
# (40*.3)+(40*.2)+(40*.2)+(40*.3) = 40.0 -> por debajo de 50
_PUNTAJES_BAJOS = {"ETHUSDT": (40, 40, 40, 40)}


def _seed_para_segunda_ronda(session, *, rsi_por_tf: dict[str, str], ratio_sube: bool) -> None:
    """ETHUSDT con RSI controlado en las 4 temporalidades, y 20 velas
    diarias de ETH y BTC para que el ratio ETH/BTC suba (RSI 100) o baje
    (RSI 0). BTC queda plano en 100."""
    tiempos = ("1h", "4h", "12h", "1d")
    candles = []
    indicadores = []
    # Cierre de la vela de hoy de ETH: continúa la serie diaria de abajo (sube
    # 1 por día hasta 120, o baja 1 por día hasta 180) para que el ratio vs
    # BTC (plano en 100) sea monótono y su RSI sea exactamente 100 o 0.
    cierre_hoy = Decimal(120 if ratio_sube else 180)
    for tf in tiempos:
        candles.append(
            Candle(
                symbol="ETHUSDT", timeframe=tf, open_time=_OPEN_TIME, open=cierre_hoy, high=cierre_hoy + 5,
                low=cierre_hoy - 5, close=cierre_hoy, volume=Decimal("1000"),
            )
        )
        indicadores.append(IndicatorRow("ETHUSDT", tf, _OPEN_TIME, "RSI_14", Decimal(rsi_por_tf[tf])))

    # Historia diaria para el ratio (incluye la vela de _OPEN_TIME ya agregada arriba para ETH)
    for i in range(1, 20):
        dia = _OPEN_TIME - timedelta(days=i)
        cierre_eth = Decimal(100 + (20 - i) if ratio_sube else 200 - (20 - i))
        candles.append(
            Candle(symbol="ETHUSDT", timeframe="1d", open_time=dia, open=cierre_eth, high=cierre_eth,
                   low=cierre_eth, close=cierre_eth, volume=Decimal("1"))
        )
    for i in range(0, 20):
        dia = _OPEN_TIME - timedelta(days=i)
        candles.append(
            Candle(symbol="BTCUSDT", timeframe="1d", open_time=dia, open=Decimal("100"), high=Decimal("100"),
                   low=Decimal("100"), close=Decimal("100"), volume=Decimal("1"))
        )
    save_candles(session, candles)
    # indicadores 1d completos salvo el RSI, que se controla arriba
    indicadores += [r for r in _indicators("ETHUSDT") if r.indicator_name != "RSI_14"]
    save_indicators(session, indicadores)


_RSI_FUERTE = {"1h": "55", "4h": "58", "12h": "60", "1d": "62"}


def _correr_ciclo(monkeypatch, db_session, puntajes):
    monkeypatch.setattr("app.orchestration.daily_cycle.run_prefilter", _fake_prefilter(["ETHUSDT"]))
    monkeypatch.setattr(
        "app.ai.agents._runner.call_agent",
        _make_fake_call_agent({"ETHUSDT": "suficiente"}, puntajes=puntajes),
    )
    monkeypatch.setattr(_DQ_PATCH_TARGET, _perfect_data_quality)
    result = run_daily_cycle(db_session, _settings(), now=_OPEN_TIME)
    (propuesta,) = get_propuestas_de_ciclo(db_session, result.ciclo_id)
    return propuesta


def test_zona_con_segunda_ronda_aprobada_queda_como_mayor_riesgo(monkeypatch, db_session):
    _seed_para_segunda_ronda(db_session, rsi_por_tf=_RSI_FUERTE, ratio_sube=True)

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_EN_ZONA)

    assert propuesta.puntaje_ponderado == Decimal("51.00")
    assert propuesta.supera_umbral_entrada is False  # el umbral 55 NO cambió
    assert propuesta.tier_entrada == TierEntrada.MAYOR_RIESGO
    assert propuesta.segunda_ronda["elegible"] is True
    assert propuesta.segunda_ronda["faltantes"] == []
    assert propuesta.decision_snapshot["tier_entrada"] == "MAYOR_RIESGO"
    assert propuesta.decision_snapshot["segunda_ronda"] == propuesta.segunda_ronda


def test_zona_con_rsi_diario_en_sobrecompra_queda_ninguno_con_evidencia(monkeypatch, db_session):
    rsi = dict(_RSI_FUERTE, **{"1d": "70.5"})
    _seed_para_segunda_ronda(db_session, rsi_por_tf=rsi, ratio_sube=True)

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_EN_ZONA)

    assert propuesta.tier_entrada == TierEntrada.NINGUNO
    assert propuesta.segunda_ronda["elegible"] is False
    assert propuesta.segunda_ronda["criterios"]["tope_sobrecompra"]["cumple"] is False


def test_zona_con_ratio_btc_debil_queda_ninguno(monkeypatch, db_session):
    _seed_para_segunda_ronda(db_session, rsi_por_tf=_RSI_FUERTE, ratio_sube=False)

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_EN_ZONA)

    assert propuesta.tier_entrada == TierEntrada.NINGUNO
    assert propuesta.segunda_ronda["criterios"]["fuerza_relativa_btc"]["cumple"] is False


def test_por_debajo_de_50_no_se_evalua_la_segunda_ronda(monkeypatch, db_session):
    _seed_para_segunda_ronda(db_session, rsi_por_tf=_RSI_FUERTE, ratio_sube=True)

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_BAJOS)

    assert propuesta.puntaje_ponderado == Decimal("40.00")
    assert propuesta.tier_entrada == TierEntrada.NINGUNO
    assert propuesta.segunda_ronda is None


# --- observación de racha: días seguidos en la zona (2026-10-04) --------------------


def _sembrar_dia_previo(session, *, dias_atras: int, puntaje: str) -> None:
    """Un día de análisis previo, exitoso, donde ETHUSDT tuvo una propuesta
    con ese puntaje ponderado."""
    from dataclasses import replace

    ciclo_id = create_ciclo(
        session, started_at=_OPEN_TIME - timedelta(days=dias_atras), status=EstadoCiclo.PREFILTER
    )
    finalizar_ciclo(
        session, ciclo_id, status=EstadoCiclo.FREEZE_DECISION_SNAPSHOT, finished_at=_OPEN_TIME,
        cost_usd=Decimal("0.5"), error_message=None,
    )
    base = PropuestaRow(
        ciclo_id=ciclo_id, symbol="ETHUSDT", categoria="principal", horizonte="corto", nivel_riesgo="medio",
        puntaje_base=Decimal(puntaje), calidad_datos=Decimal(100), puntaje_ponderado=Decimal(puntaje),
        supera_umbral_entrada=False, tesis="t", invalidacion="i",
        escenario_adverso={"descripcion": "x", "desencadenante": "y", "probabilidad_relativa": "baja"},
        comentario_chartista=None, fuentes=[], confianza_global="alta", decision_snapshot={}, factores=(),
    )
    save_propuesta(session, replace(base))


def test_racha_cuenta_los_dias_previos_en_zona_y_el_actual(monkeypatch, db_session):
    _seed_para_segunda_ronda(db_session, rsi_por_tf=_RSI_FUERTE, ratio_sube=True)
    _sembrar_dia_previo(db_session, dias_atras=1, puntaje="52.00")
    _sembrar_dia_previo(db_session, dias_atras=2, puntaje="50.50")
    _sembrar_dia_previo(db_session, dias_atras=3, puntaje="47.00")  # fuera de zona: corta la racha

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_EN_ZONA)  # hoy: 51.00

    assert propuesta.ciclos_consecutivos_en_zona == 3  # hoy + 2 previos
    assert propuesta.decision_snapshot["ciclos_consecutivos_en_zona"] == 3


def test_racha_larga_no_promueve_el_tier_si_la_segunda_ronda_no_aprueba(monkeypatch, db_session):
    # La racha es SOLO observación: aunque lleve 3 días en la zona, si el
    # ratio vs BTC es débil el tier sigue siendo NINGUNO.
    _seed_para_segunda_ronda(db_session, rsi_por_tf=_RSI_FUERTE, ratio_sube=False)
    _sembrar_dia_previo(db_session, dias_atras=1, puntaje="52.00")
    _sembrar_dia_previo(db_session, dias_atras=2, puntaje="50.50")

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_EN_ZONA)

    assert propuesta.ciclos_consecutivos_en_zona == 3
    assert propuesta.tier_entrada == TierEntrada.NINGUNO


def test_racha_es_cero_si_hoy_no_esta_en_la_zona(monkeypatch, db_session):
    _seed_para_segunda_ronda(db_session, rsi_por_tf=_RSI_FUERTE, ratio_sube=True)
    _sembrar_dia_previo(db_session, dias_atras=1, puntaje="52.00")

    propuesta = _correr_ciclo(monkeypatch, db_session, _PUNTAJES_BAJOS)  # hoy: 40.00

    assert propuesta.ciclos_consecutivos_en_zona == 0
