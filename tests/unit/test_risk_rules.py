"""Tests de app.domain.rules.risk — el único componente del plan con
cobertura exigida al 100% (sección 9): umbrales EXACTOS, un paso antes y un
paso después de cada uno.

Cubre los casos obligatorios de la sección 15.8 del plan que son reglas
puras: stop exacto en -8%, TP del 50% en +12%, trailing del 6% después de
activarse y drawdown global de -15%. Los casos que necesitan base de datos
(reintento sin duplicación, reinicio con posición abierta) se prueban cuando
existan los servicios de `app/execution/`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.enums import IntencionOrden, TierEntrada
from app.domain.rules import risk
from app.domain.rules.risk import (
    AccionMonitor,
    EstadoParaMonitor,
    MotivoRechazoEntrada,
    SituacionDeCupos,
    calculate_drawdown_pct,
    calculate_entry_quantity,
    calculate_fee,
    calculate_position_value,
    calculate_initial_levels,
    evaluate_position,
    is_drawdown_limit_reached,
    update_peak_equity,
    validate_entry,
)

D = Decimal
PASO = D("0.000000000001")  # 1E-12: la escala de NUMERIC(30,12)


# --- constantes fijas del plan -------------------------------------------


def test_las_reglas_fijas_coinciden_con_la_seccion_9_del_plan():
    assert risk.STOP_LOSS_PCT == D("0.08")
    assert risk.TAKE_PROFIT_PCT == D("0.12")
    assert risk.TAKE_PROFIT_FRACCION_A_CERRAR == D("0.5")
    assert risk.TRAILING_PCT == D("0.06")
    assert risk.MAX_DRAWDOWN_PCT == D("0.15")
    assert risk.MAX_POSICIONES_ABIERTAS == 4
    assert risk.MAX_ENTRADAS_POR_DIA == 3
    assert risk.UMBRAL_APROBACION_MANUAL_USD == 200
    assert risk.TAMANO_MINIMO_USD == 100
    assert risk.TAMANO_MAXIMO_USD == 250
    assert risk.COMISION_POR_LADO == D("0.001")


def test_decisiones_del_2026_10_04():
    assert risk.TAMANO_POSICION_USD == 100  # tamaño fijo en el mínimo
    assert risk.MAX_MAYOR_RIESGO_ABIERTAS == 1  # tope propio de mayor riesgo


# --- niveles iniciales ----------------------------------------------------


def test_niveles_iniciales_desde_100():
    niveles = calculate_initial_levels(D(100))
    assert niveles.stop_price == D(92)
    assert niveles.target_price == D(112)


def test_niveles_iniciales_con_precio_realista_son_exactos_y_a_12_decimales():
    niveles = calculate_initial_levels(D("81234.56789"))
    assert niveles.stop_price == D("74735.8024588")  # 81234.56789 × 0.92, a mano
    assert niveles.target_price == D("90982.7160368")  # × 1.12 exacto
    assert niveles.stop_price.as_tuple().exponent == -12


def test_niveles_con_precio_de_12_decimales_se_redondean_a_la_escala_de_la_columna():
    niveles = calculate_initial_levels(D("0.000012345678"))  # tipo SHIB
    assert niveles.stop_price == D("0.000011358024")
    assert niveles.target_price == D("0.000013827159")
    assert niveles.stop_price.as_tuple().exponent == -12


@pytest.mark.parametrize("precio", [D(0), D(-1)])
def test_niveles_iniciales_rechazan_precio_no_positivo(precio):
    with pytest.raises(ValueError):
        calculate_initial_levels(precio)


# --- cantidad y comisión --------------------------------------------------


def test_cantidad_de_entrada_100_usd_a_precio_redondo():
    assert calculate_entry_quantity(D(100), D(50)) == D(2)


def test_cantidad_de_entrada_redondea_hacia_abajo_nunca_compra_de_mas():
    cantidad = calculate_entry_quantity(D(100), D(3))
    assert cantidad == D("33.333333333333")
    assert cantidad * D(3) <= D(100)


def test_cantidad_de_entrada_con_precio_alto():
    cantidad = calculate_entry_quantity(D(100), D("81234.56789"))
    assert cantidad * D("81234.56789") <= D(100)
    assert (cantidad + PASO) * D("81234.56789") > D(100)  # es la mayor cantidad posible


def test_comision_es_el_0_1_por_ciento_del_valor():
    assert calculate_fee(D(1), D(100)) == D("0.10000000")
    assert calculate_fee(D("2"), D("50")) == D("0.10000000")


def test_comision_se_redondea_a_8_decimales():
    # 0.000123456789 × 81000.5 × 0.001 = 0.00999999...
    fee = calculate_fee(D("0.000123456789"), D("81000.5"))
    assert fee.as_tuple().exponent == -8


@pytest.mark.parametrize("args", [(D(0), D(1)), (D(1), D(0)), (D(-1), D(1))])
def test_comision_y_cantidad_rechazan_valores_no_positivos(args):
    with pytest.raises(ValueError):
        calculate_fee(*args)
    with pytest.raises(ValueError):
        calculate_entry_quantity(*args)


# --- monitoreo: sin TP ejecutado -----------------------------------------


def _estado(**overrides) -> EstadoParaMonitor:
    niveles = calculate_initial_levels(D(100))
    defaults = dict(
        remaining_quantity=D(1),
        initial_stop_price=niveles.stop_price,
        initial_target_price=niveles.target_price,
        take_profit_hit=False,
        trailing_active=False,
        trailing_peak_price=None,
    )
    defaults.update(overrides)
    return EstadoParaMonitor(**defaults)


def _estado_con_trailing(peak: Decimal, cantidad: Decimal = D("0.5")) -> EstadoParaMonitor:
    return _estado(
        remaining_quantity=cantidad, take_profit_hit=True, trailing_active=True, trailing_peak_price=peak
    )


def test_precio_entre_stop_y_objetivo_no_hace_nada():
    for precio in (D("92.000000000001"), D(100), D("111.999999999999")):
        decision = evaluate_position(_estado(), precio)
        assert decision.accion == AccionMonitor.MANTENER
        assert decision.intencion is None
        assert decision.cantidad_a_vender == 0
        assert decision.trailing_active is False
        assert decision.trailing_peak_price is None


def test_stop_exacto_en_menos_8_por_ciento_cierra_todo():
    decision = evaluate_position(_estado(remaining_quantity=D("1.5")), D(92))
    assert decision.accion == AccionMonitor.CERRAR_TOTAL
    assert decision.intencion == IntencionOrden.STOP_LOSS
    assert decision.cantidad_a_vender == D("1.5")


def test_un_paso_antes_del_stop_no_dispara_y_un_paso_despues_si():
    assert evaluate_position(_estado(), D(92) + PASO).accion == AccionMonitor.MANTENER
    assert evaluate_position(_estado(), D(92) - PASO).accion == AccionMonitor.CERRAR_TOTAL


def test_si_el_precio_salta_por_debajo_del_stop_igual_dispara_el_stop():
    decision = evaluate_position(_estado(), D(85))
    assert decision.intencion == IntencionOrden.STOP_LOSS
    assert decision.cantidad_a_vender == D(1)


def test_take_profit_exacto_en_mas_12_por_ciento_cierra_el_50_por_ciento():
    decision = evaluate_position(_estado(remaining_quantity=D(2)), D(112))
    assert decision.accion == AccionMonitor.CERRAR_PARCIAL
    assert decision.intencion == IntencionOrden.TAKE_PROFIT_PARTIAL
    assert decision.cantidad_a_vender == D(1)
    assert decision.take_profit_hit is True
    assert decision.trailing_active is True
    assert decision.trailing_peak_price == D(112)  # primer máximo = precio observado
    assert decision.trailing_stop_price == D("105.28")


def test_un_paso_antes_del_objetivo_no_dispara_y_un_paso_despues_si():
    assert evaluate_position(_estado(), D(112) - PASO).accion == AccionMonitor.MANTENER
    assert evaluate_position(_estado(), D(112) + PASO).accion == AccionMonitor.CERRAR_PARCIAL


def test_si_el_precio_salta_por_encima_del_objetivo_el_trailing_arranca_desde_ese_precio():
    decision = evaluate_position(_estado(), D(120))
    assert decision.accion == AccionMonitor.CERRAR_PARCIAL
    assert decision.trailing_peak_price == D(120)
    assert decision.trailing_stop_price == D("112.8")


def test_el_cierre_parcial_redondea_hacia_abajo_y_nunca_vende_todo():
    decision = evaluate_position(_estado(remaining_quantity=D("0.000000000003")), D(112))
    assert decision.cantidad_a_vender == D("0.000000000001")
    assert decision.cantidad_a_vender < D("0.000000000003")


def test_una_cantidad_demasiado_pequena_para_cierre_parcial_se_rechaza():
    with pytest.raises(ValueError, match="demasiado pequeña"):
        evaluate_position(_estado(remaining_quantity=PASO), D(112))


# --- monitoreo: trailing activo ------------------------------------------


def test_trailing_exacto_en_6_por_ciento_desde_el_maximo_cierra_el_resto():
    decision = evaluate_position(_estado_con_trailing(D(120)), D("112.8"))
    assert decision.accion == AccionMonitor.CERRAR_TOTAL
    assert decision.intencion == IntencionOrden.TRAILING_STOP
    assert decision.cantidad_a_vender == D("0.5")
    assert decision.trailing_peak_price == D(120)


def test_un_paso_antes_del_trailing_no_dispara_y_un_paso_despues_si():
    assert evaluate_position(_estado_con_trailing(D(120)), D("112.8") + PASO).accion == AccionMonitor.MANTENER
    assert evaluate_position(_estado_con_trailing(D(120)), D("112.8") - PASO).accion == AccionMonitor.CERRAR_TOTAL


def test_el_maximo_sube_con_el_precio_y_el_nivel_del_trailing_sube_con_el_():
    decision = evaluate_position(_estado_con_trailing(D(120)), D(125))
    assert decision.accion == AccionMonitor.MANTENER
    assert decision.trailing_peak_price == D(125)
    assert decision.trailing_stop_price == D("117.5")


def test_el_maximo_nunca_baja_aunque_el_precio_retroceda():
    decision = evaluate_position(_estado_con_trailing(D(125)), D(118))  # 118 > 117.5
    assert decision.accion == AccionMonitor.MANTENER
    assert decision.trailing_peak_price == D(125)
    assert decision.trailing_stop_price == D("117.5")


def test_trailing_se_mide_con_el_maximo_ya_actualizado_por_el_precio_actual():
    # Precio por encima del máximo guardado: nunca puede disparar contra sí mismo.
    decision = evaluate_position(_estado_con_trailing(D(120)), D(130))
    assert decision.accion == AccionMonitor.MANTENER
    assert decision.trailing_peak_price == D(130)


def test_una_caida_brusca_con_trailing_activo_sale_por_trailing_no_por_stop():
    decision = evaluate_position(_estado_con_trailing(D(120)), D(85))  # bajo el stop inicial de 92
    assert decision.intencion == IntencionOrden.TRAILING_STOP
    assert decision.cantidad_a_vender == D("0.5")


def test_con_tp_ejecutado_el_stop_inicial_ya_no_interviene():
    # 100 está por encima del stop inicial (92) pero bajo el trailing (105.28).
    decision = evaluate_position(_estado_con_trailing(D(112)), D(100))
    assert decision.intencion == IntencionOrden.TRAILING_STOP


def test_el_trailing_siempre_queda_por_encima_del_stop_inicial_para_cualquier_maximo_posible():
    niveles = calculate_initial_levels(D(100))
    for peak in (niveles.target_price, D("112.000000000001"), D(150), D(1000)):
        decision = evaluate_position(_estado_con_trailing(peak), peak)
        assert decision.trailing_stop_price > niveles.stop_price


def test_ninguna_evaluacion_con_tp_ejecutado_produce_stop_loss():
    for precio in range(1, 300):
        decision = evaluate_position(_estado_con_trailing(D(130)), D(precio))
        assert decision.intencion in (None, IntencionOrden.TRAILING_STOP)


# --- estados incoherentes -------------------------------------------------


def test_trailing_sin_tp_ejecutado_es_incoherente():
    with pytest.raises(ValueError, match="incoherente"):
        evaluate_position(_estado(trailing_active=True, trailing_peak_price=D(110)), D(100))


def test_tp_ejecutado_sin_trailing_es_incoherente():
    with pytest.raises(ValueError, match="incoherente"):
        evaluate_position(_estado(take_profit_hit=True), D(100))


def test_trailing_sin_precio_maximo_es_incoherente():
    with pytest.raises(ValueError, match="trailing_peak_price"):
        evaluate_position(_estado(take_profit_hit=True, trailing_active=True), D(100))


@pytest.mark.parametrize("precio", [D(0), D(-5)])
def test_precio_observado_no_positivo_se_rechaza(precio):
    with pytest.raises(ValueError):
        evaluate_position(_estado(), precio)


def test_posicion_sin_cantidad_se_rechaza():
    with pytest.raises(ValueError):
        evaluate_position(_estado(remaining_quantity=D(0)), D(100))


def test_resultados_son_decimal_nunca_float():
    decision = evaluate_position(_estado(), D(112))
    assert isinstance(decision.cantidad_a_vender, Decimal)
    assert isinstance(decision.trailing_peak_price, Decimal)
    assert isinstance(decision.trailing_stop_price, Decimal)


def test_la_evaluacion_es_determinista():
    estado = _estado_con_trailing(D(120))
    assert evaluate_position(estado, D("113.5")) == evaluate_position(estado, D("113.5"))


# --- drawdown -------------------------------------------------------------


def test_drawdown_exacto_en_menos_15_por_ciento_pausa():
    assert is_drawdown_limit_reached(D(850), D(1000)) is True


def test_un_paso_antes_del_drawdown_no_pausa_y_un_paso_despues_si():
    assert is_drawdown_limit_reached(D("850.00000001"), D(1000)) is False
    assert is_drawdown_limit_reached(D("849.99999999"), D(1000)) is True


def test_el_drawdown_se_mide_contra_el_pico_no_contra_el_capital_inicial():
    # Pico 1200: el límite es 1020, aunque 1020 esté sobre los 1.000 iniciales.
    assert is_drawdown_limit_reached(D(1020), D(1200)) is True
    assert is_drawdown_limit_reached(D("1020.00000001"), D(1200)) is False


def test_equity_sobre_el_pico_nunca_pausa_y_el_pico_sube():
    assert is_drawdown_limit_reached(D(1300), D(1000)) is False
    assert update_peak_equity(D(1000), D(1300)) == D(1300)
    assert update_peak_equity(D(1300), D(900)) == D(1300)  # nunca baja


def test_porcentaje_de_drawdown_para_reportar():
    assert calculate_drawdown_pct(D(850), D(1000)) == D("-15.000000")
    assert calculate_drawdown_pct(D(950), D(1000)) == D("-5.000000")
    assert calculate_drawdown_pct(D(1100), D(1000)) == D("0E-6")  # sobre el pico: 0, nunca positivo


def test_drawdown_rechaza_valores_invalidos():
    with pytest.raises(ValueError):
        is_drawdown_limit_reached(D(-1), D(1000))
    with pytest.raises(ValueError):
        is_drawdown_limit_reached(D(500), D(0))


# --- validación de entradas -----------------------------------------------


def _cupos(**overrides) -> SituacionDeCupos:
    defaults = dict(
        posiciones_abiertas=0,
        entradas_hoy=0,
        mayor_riesgo_abiertas=0,
        simbolos_abiertos=frozenset(),
        sistema_pausado=False,
    )
    defaults.update(overrides)
    return SituacionDeCupos(**defaults)


def test_entrada_normal_de_100_usd_se_permite_y_se_ejecuta_sola():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos())
    assert v.permitida is True
    assert v.requiere_aprobacion_manual is False
    assert v.monto_usd == D(100)
    assert v.motivos_rechazo == ()


def test_entrada_de_mayor_riesgo_siempre_exige_aprobacion_manual():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.MAYOR_RIESGO, cupos=_cupos())
    assert v.permitida is True
    assert v.requiere_aprobacion_manual is True
    assert v.monto_usd == D(100)


def test_umbral_de_aprobacion_manual_es_estrictamente_sobre_200_usd():
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(), monto_usd=D(200)).requiere_aprobacion_manual is False
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(), monto_usd=D("200.00000001")).requiere_aprobacion_manual is True


@pytest.mark.parametrize("monto,permitida", [(D("99.99999999"), False), (D(100), True), (D(250), True), (D("250.00000001"), False)])
def test_tamano_fuera_del_rango_100_a_250_se_rechaza(monto, permitida):
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(), monto_usd=monto)
    assert v.permitida is permitida
    if not permitida:
        assert v.motivos_rechazo == (MotivoRechazoEntrada.TAMANO_FUERA_DE_RANGO,)


def test_simbolo_fuera_de_la_whitelist_se_rechaza():
    v = validate_entry(symbol="FAKEUSDT", tier=TierEntrada.NORMAL, cupos=_cupos())
    assert v.permitida is False
    assert MotivoRechazoEntrada.SIMBOLO_NO_PERMITIDO in v.motivos_rechazo


def test_un_simbolo_dinamico_o_con_formato_distinto_tampoco_pasa():
    for simbolo in ("btcusdt", "BTC/USDT", "BTCUSDT ", ""):
        assert validate_entry(symbol=simbolo, tier=TierEntrada.NORMAL, cupos=_cupos()).permitida is False


def test_tier_ninguno_no_califica():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NINGUNO, cupos=_cupos())
    assert v.permitida is False
    assert MotivoRechazoEntrada.TIER_NO_CALIFICA in v.motivos_rechazo


def test_sistema_pausado_bloquea_toda_entrada_nueva():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(sistema_pausado=True))
    assert v.motivos_rechazo == (MotivoRechazoEntrada.SISTEMA_PAUSADO,)


def test_nunca_se_abre_un_simbolo_que_ya_tiene_posicion():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(simbolos_abiertos=frozenset({"BTCUSDT"})))
    assert v.motivos_rechazo == (MotivoRechazoEntrada.SIMBOLO_YA_ABIERTO,)
    # otro símbolo abierto no estorba
    assert validate_entry(symbol="ETHUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(simbolos_abiertos=frozenset({"BTCUSDT"}))).permitida


def test_maximo_de_4_posiciones_abiertas():
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(posiciones_abiertas=3)).permitida is True
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(posiciones_abiertas=4))
    assert v.motivos_rechazo == (MotivoRechazoEntrada.MAXIMO_POSICIONES,)


def test_maximo_de_3_entradas_por_dia():
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(entradas_hoy=2)).permitida is True
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(entradas_hoy=3))
    assert v.motivos_rechazo == (MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA,)


def test_tope_propio_de_una_entrada_de_mayor_riesgo_abierta_a_la_vez():
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.MAYOR_RIESGO, cupos=_cupos(mayor_riesgo_abiertas=0)).permitida is True
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.MAYOR_RIESGO, cupos=_cupos(mayor_riesgo_abiertas=1))
    assert v.motivos_rechazo == (MotivoRechazoEntrada.MAXIMO_MAYOR_RIESGO,)


def test_el_tope_de_mayor_riesgo_no_frena_a_las_entradas_normales():
    assert validate_entry(symbol="BTCUSDT", tier=TierEntrada.NORMAL, cupos=_cupos(mayor_riesgo_abiertas=1)).permitida is True


def test_se_devuelven_todos_los_motivos_de_rechazo_no_solo_el_primero():
    v = validate_entry(
        symbol="FAKEUSDT",
        tier=TierEntrada.MAYOR_RIESGO,
        cupos=_cupos(
            posiciones_abiertas=4,
            entradas_hoy=3,
            mayor_riesgo_abiertas=1,
            sistema_pausado=True,
        ),
        monto_usd=D(50),
    )
    assert set(v.motivos_rechazo) == {
        MotivoRechazoEntrada.SISTEMA_PAUSADO,
        MotivoRechazoEntrada.SIMBOLO_NO_PERMITIDO,
        MotivoRechazoEntrada.MAXIMO_POSICIONES,
        MotivoRechazoEntrada.MAXIMO_ENTRADAS_DIA,
        MotivoRechazoEntrada.MAXIMO_MAYOR_RIESGO,
        MotivoRechazoEntrada.TAMANO_FUERA_DE_RANGO,
    }
    assert v.permitida is False


def test_una_entrada_rechazada_igual_informa_si_habria_requerido_aprobacion():
    v = validate_entry(symbol="BTCUSDT", tier=TierEntrada.MAYOR_RIESGO, cupos=_cupos(sistema_pausado=True))
    assert v.permitida is False
    assert v.requiere_aprobacion_manual is True


def test_el_default_de_settings_no_se_desalinea_de_la_regla_fija_de_posiciones(monkeypatch):
    # `Settings.max_open_positions` (variable de entorno heredada del plan,
    # sección 15.4) NO es la fuente de verdad del riesgo: lo es esta
    # constante de código (sección 10: las reglas críticas no se cambian
    # desde `.env`). Este test falla si alguien cambia uno sin el otro.
    from app.config import Settings

    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://x:x@localhost/x")  # type: ignore[call-arg]
    assert settings.max_open_positions == risk.MAX_POSICIONES_ABIERTAS


# --- sistema_pausado (estado de la pausa por drawdown) ---


def test_sistema_pausado_depende_solo_del_ultimo_evento():
    from app.domain.enums import TipoEventoSistema
    from app.domain.rules.risk import sistema_pausado

    assert sistema_pausado(None) is False
    assert sistema_pausado(TipoEventoSistema.PAUSA_DRAWDOWN) is True
    assert sistema_pausado(TipoEventoSistema.REANUDACION) is False


def test_calculate_trailing_stop_es_maximo_por_94_porciento_y_rechaza_no_positivo():
    from app.domain.rules.risk import calculate_trailing_stop

    assert calculate_trailing_stop(D("120")) == D("112.8")
    assert calculate_trailing_stop(D("112")) == D("105.28")
    with pytest.raises(ValueError):
        calculate_trailing_stop(D("0"))


def test_valor_de_la_posicion_es_cantidad_por_precio_sin_comision():
    assert calculate_position_value(D("2.5"), D("3.333")) == D("8.33250000")  # cantidad x precio, a 8 decimales
    assert calculate_position_value(D(1), D(100)) == D("100.00000000")
    with pytest.raises(ValueError):
        calculate_position_value(D(0), D(100))
    with pytest.raises(ValueError):
        calculate_position_value(D(1), D(-1))
