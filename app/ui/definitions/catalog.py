"""Catálogo interno y versionado de definiciones (sección 8.1 del plan).

Cada indicador o métrica que la interfaz muestra se explica con un tooltip.
Los tooltips son EXCLUSIVAMENTE explicativos: no calculan nada, no cambian
pesos, decisiones ni reglas de ejecución. El texto vive aquí, en código
versionado en Git, para que la explicación sea la misma en todas las
pantallas y reproducible históricamente: si una definición cambia, se sube
su `version` (y `CATALOG_VERSION`); un test lo exige comparando una huella
del contenido.

Contrato mínimo del plan: key, display_name, short_description,
interpretation, unit, version. Se agregan dos campos que la sección 8.1 pide
explicar ("limitaciones relevantes" y "si es un dato de decisión o una
métrica de evaluación posterior"): `limitations` y `kind`.

Este módulo no importa Streamlit: es texto puro y se prueba sin interfaz.
Las definiciones describen lo que el sistema HACE hoy, no teoría general; por
eso mencionan los parámetros reales (periodo 14, SMA de 30 periodos, etc.).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum


class TipoDeDato(str, Enum):
    DECISION = "DECISION"
    """Entra al análisis o al score ANTES de abrir una operación."""
    EVALUACION_POSTERIOR = "EVALUACION_POSTERIOR"
    """Se mide DESPUÉS, para evaluar la decisión; nunca la altera."""


@dataclass(frozen=True)
class Definition:
    key: str
    display_name: str
    short_description: str
    interpretation: str
    unit: str
    limitations: str
    kind: TipoDeDato
    version: str


CATALOG_VERSION = "1"

_DEFINICIONES: tuple[Definition, ...] = (
    Definition(
        key="rsi",
        display_name="RSI (índice de fuerza relativa)",
        short_description=(
            "Mide qué tan fuertes han sido las subidas recientes frente a las bajadas, de 0 a 100. "
            "Se calcula con periodo 14 y método de Wilder, en 1h, 4h, 12h y diaria."
        ),
        interpretation=(
            "Por encima de 70 suele leerse como zona de sobrecompra y por debajo de 30 como sobreventa. "
            "En este sistema el Analista cuantitativo lo compara entre temporalidades y busca conflictos "
            "entre ellas; no es una señal de compra o venta por sí solo."
        ),
        unit="Escala 0 a 100 (sin unidad)",
        limitations=(
            "Depende de la temporalidad. En tendencias fuertes puede permanecer mucho tiempo en zonas "
            "extremas. Se calcula sobre las velas guardadas, por lo que puede tener horas de antigüedad."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="atr",
        display_name="ATR (rango verdadero medio)",
        short_description=(
            "Tamaño típico del movimiento de una vela, incluyendo huecos. Periodo 14, método de Wilder, "
            "en las cuatro temporalidades."
        ),
        interpretation=(
            "Un ATR alto frente al precio indica un régimen volátil; uno bajo, un mercado tranquilo. "
            "El Analista cuantitativo lo usa para describir el régimen de volatilidad."
        ),
        unit="Unidades de precio del activo (USDT)",
        limitations=(
            "No indica dirección. Al ser un valor absoluto, no es comparable entre activos sin dividirlo "
            "por el precio. No define el stop: el stop del sistema es fijo (-8% desde la entrada)."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="relative_volume",
        display_name="Volumen relativo",
        short_description=(
            "Volumen de la vela más reciente dividido por el promedio simple del volumen de los últimos "
            "30 periodos de esa misma temporalidad."
        ),
        interpretation=(
            "1,0 significa volumen en su media. Cerca de máximos, un valor por encima de 2,5 sugiere "
            "continuación del movimiento; con valores menores, aumenta la probabilidad de retroceso "
            "a corto plazo. El Analista cuantitativo siempre lo evalúa junto con la posición del precio."
        ),
        unit="Múltiplo de la media (x)",
        limitations=(
            "Si la vela más reciente aún no ha cerrado, su volumen parcial puede hacer que el valor se vea "
            "bajo. Cada temporalidad se compara contra su propia media, no contra una referencia diaria."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="moving_averages",
        display_name="Medias móviles 90/120/180",
        short_description=(
            "Promedio simple (SMA, no exponencial) del cierre de los últimos 90, 120 y 180 periodos de "
            "cada temporalidad. En la temporalidad diaria equivalen a 90, 120 y 180 días."
        ),
        interpretation=(
            "Precio por encima de las tres medias indica tendencia de fondo alcista; muy por encima, "
            "una posible extensión excesiva. Que las medias estén ordenadas (90 sobre 120 sobre 180) "
            "refuerza la lectura de tendencia."
        ),
        unit="Precio (USDT)",
        limitations=(
            "Son indicadores rezagados: confirman la tendencia después de que ocurrió. En 1h, 4h y 12h los "
            "periodos son horas o bloques de horas, no días."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="distance_ath",
        display_name="Distancia al máximo histórico",
        short_description=(
            "(Cierre - máximo) / máximo x 100, calculado solo en velas diarias, sobre el máximo más alto "
            "de la historia guardada."
        ),
        interpretation=(
            "Cero o cercano a cero significa que el precio está en su máximo; -34 significa 34% por debajo. "
            "Estar cerca del máximo eleva la probabilidad de retroceso a corto plazo, salvo que el volumen "
            "sea extraordinario."
        ),
        unit="Porcentaje (normalmente negativo o cero)",
        limitations=(
            "El 'máximo histórico' es el más alto dentro de las velas guardadas (aproximadamente 2 años), "
            "no el máximo absoluto del activo desde su lanzamiento."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="distance_90d_high",
        display_name="Distancia al máximo de 90 días",
        short_description=(
            "(Cierre - máximo de las últimas 90 velas diarias) / ese máximo x 100. Solo en temporalidad diaria."
        ),
        interpretation=(
            "Mide qué tan lejos está el precio de su techo reciente. Valores cercanos a cero indican que "
            "está probando ese techo, una zona donde el sistema exige confirmación de volumen."
        ),
        unit="Porcentaje (normalmente negativo o cero)",
        limitations="Depende de la ventana fija de 90 días; un techo anterior a esa ventana no cuenta.",
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="mae",
        display_name="MAE (máxima excursión adversa)",
        short_description=(
            "La peor caída que tuvo la operación contra la entrada mientras estuvo abierta: "
            "(precio mínimo observado - precio de entrada) / precio de entrada x 100."
        ),
        interpretation=(
            "Muestra cuánto dolor soportó la operación antes de cerrar. Un MAE cercano al stop (-8%) en una "
            "operación ganadora indica que estuvo cerca de perderse."
        ),
        unit="Porcentaje (negativo o cero)",
        limitations=(
            "Se calcula con los precios observados cada 60 segundos por el monitor; un movimiento más "
            "extremo entre dos observaciones no queda registrado."
        ),
        kind=TipoDeDato.EVALUACION_POSTERIOR,
        version="1",
    ),
    Definition(
        key="mfe",
        display_name="MFE (máxima excursión favorable)",
        short_description=(
            "La mejor ganancia latente que tuvo la operación mientras estuvo abierta: "
            "(precio máximo observado - precio de entrada) / precio de entrada x 100."
        ),
        interpretation=(
            "Compara lo que la operación llegó a ganar con lo que finalmente se capturó. Un MFE alto con un "
            "resultado bajo sugiere que la salida devolvió mucha ganancia."
        ),
        unit="Porcentaje (positivo o cero)",
        limitations="Mismas limitaciones que el MAE: solo ve los precios observados cada 60 segundos.",
        kind=TipoDeDato.EVALUACION_POSTERIOR,
        version="1",
    ),
    Definition(
        key="r_multiple",
        display_name="R-multiple",
        short_description=(
            "Resultado neto de la operación medido en unidades de riesgo inicial: P&L neto dividido por "
            "(distancia entre entrada y stop inicial x cantidad)."
        ),
        interpretation=(
            "+1R significa que se ganó lo mismo que se arriesgaba; -1R, que se perdió todo el riesgo "
            "planeado. Permite comparar operaciones de distinto tamaño."
        ),
        unit="Múltiplo de R (x)",
        limitations=(
            "Usa el P&L neto (con comisiones), por lo que una operación cerrada exactamente en el stop "
            "queda ligeramente por debajo de -1R."
        ),
        kind=TipoDeDato.EVALUACION_POSTERIOR,
        version="1",
    ),
    Definition(
        key="expectancy",
        display_name="Expectancy (expectativa por operación)",
        short_description=(
            "Ganancia o pérdida neta promedio por operación (P&L neto total dividido por el número de "
            "operaciones). También se guarda su forma porcentual."
        ),
        interpretation=(
            "Positiva significa que, en promedio, cada operación dejó dinero después de costos. Es más "
            "informativa que el porcentaje de aciertos: pocas ganancias grandes pueden compensar muchas "
            "pérdidas pequeñas."
        ),
        unit="USD por operación (y porcentaje)",
        limitations=(
            "Con pocas operaciones (menos de 30) es muy inestable y no debe tomarse como evidencia; "
            "el plan fija 30 operaciones como mínimo para una conclusión."
        ),
        kind=TipoDeDato.EVALUACION_POSTERIOR,
        version="1",
    ),
    Definition(
        key="profit_factor",
        display_name="Profit Factor",
        short_description=(
            "Suma de los P&L netos positivos dividida por el valor absoluto de la suma de los negativos."
        ),
        interpretation=(
            "Mayor que 1 significa que lo ganado supera lo perdido; igual a 1, empate. Por ejemplo, 1,5 "
            "indica 1,5 USD ganados por cada USD perdido."
        ),
        unit="Razón (x)",
        limitations=(
            "Si no hay operaciones perdedoras no se calcula y se muestra como no disponible, no como un "
            "número inventado. Con pocas operaciones es muy inestable."
        ),
        kind=TipoDeDato.EVALUACION_POSTERIOR,
        version="1",
    ),
    Definition(
        key="max_drawdown",
        display_name="Máximo drawdown",
        short_description=(
            "La mayor caída porcentual del capital desde un pico hasta un valle posterior, sobre la curva "
            "de equity (capital + P&L realizado + P&L no realizado), registrada cada 60 segundos."
        ),
        interpretation=(
            "Mide el peor momento que habría vivido el capital. El sistema se pausa para nuevas entradas "
            "si el equity cae un 15% desde su pico."
        ),
        unit="Porcentaje (negativo o cero)",
        limitations=(
            "Es una propiedad de la trayectoria del capital, no de la peor operación individual. "
            "Solo existe para el alcance global."
        ),
        kind=TipoDeDato.EVALUACION_POSTERIOR,
        version="1",
    ),
    Definition(
        key="opportunity_score",
        display_name="Opportunity Score (puntaje de oportunidad)",
        short_description=(
            "Puntaje de 0 a 100 de una propuesta: el Consolidador juzga cuatro categorías (tendencia y "
            "momentum 30%, volumen y liquidez 20%, posición frente a rangos 20%, contexto de eventos 30%) "
            "y el código calcula la suma ponderada, multiplicada por la calidad de los datos."
        ),
        interpretation=(
            "El sistema solo considera entrar con 55 o más. Es el puntaje cuyo poder predictivo se evalúa: "
            "se compara el tercio superior contra el inferior de las operaciones cerradas."
        ),
        unit="Escala 0 a 100",
        limitations=(
            "El modelo de IA entrega su juicio por categoría, pero la aritmética final es determinista. "
            "Los pesos son una base inicial y no se ajustan después de ver resultados."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="risk_score",
        display_name="Risk Score (puntaje de riesgo)",
        short_description=(
            "El plan prevé un puntaje numérico de riesgo para cada decisión. En esta versión el sistema "
            "no lo calcula: el riesgo se expresa como un nivel cualitativo en la propuesta."
        ),
        interpretation=(
            "Hoy se muestra como no disponible. El riesgo efectivo de cada operación lo acotan las reglas "
            "fijas de riesgo (stop -8%, tamaño fijo de 100 USD, máximo 4 posiciones)."
        ),
        unit="Escala 0 a 100 (pendiente)",
        limitations=(
            "No hay fórmula definida todavía; no se rellena con un valor inventado. Se definirá con datos "
            "reales."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
    Definition(
        key="confidence_score",
        display_name="Confidence Score (puntaje de confianza)",
        short_description=(
            "El plan prevé un puntaje numérico de confianza. En esta versión la confianza es cualitativa "
            "(alta, media o baja) y viene del Consolidador; no hay un número calculado."
        ),
        interpretation=(
            "Hoy se muestra como no disponible en las métricas. La calidad de los datos sí entra como "
            "multiplicador numérico dentro del Opportunity Score."
        ),
        unit="Escala 0 a 100 (pendiente)",
        limitations=(
            "No hay fórmula definida todavía; no se rellena con un valor inventado. Se definirá con datos "
            "reales."
        ),
        kind=TipoDeDato.DECISION,
        version="1",
    ),
)


class DefinitionCatalog:
    """Acceso de solo lectura al catálogo."""

    def __init__(self, definiciones: tuple[Definition, ...], version: str) -> None:
        claves = [d.key for d in definiciones]
        if len(set(claves)) != len(claves):
            raise ValueError("El catálogo de definiciones tiene claves repetidas.")
        self._por_clave = {d.key: d for d in definiciones}
        self.version = version

    def keys(self) -> list[str]:
        return list(self._por_clave)

    def get(self, key: str) -> Definition | None:
        return self._por_clave.get(key)

    def tooltip_text(self, key: str) -> str | None:
        """Texto plano del tooltip, o `None` si la clave no existe (la interfaz
        entonces no muestra tooltip en vez de inventar uno)."""
        d = self.get(key)
        if d is None:
            return None
        tipo = "Dato de decisión" if d.kind == TipoDeDato.DECISION else "Métrica de evaluación posterior"
        return (
            f"{d.short_description}\n\nCómo leerlo: {d.interpretation}\n\n"
            f"Unidad: {d.unit}\n\nLimitaciones: {d.limitations}\n\n{tipo} · definición v{d.version}"
        )

    def fingerprint(self) -> str:
        """Huella del contenido completo; un test la compara para exigir que
        cualquier cambio de texto vaya acompañado de una nueva versión."""
        contenido = [{**asdict(d), "kind": d.kind.value} for d in self._por_clave.values()]
        crudo = json.dumps({"version": self.version, "definiciones": contenido}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(crudo.encode("utf-8")).hexdigest()


DEFINITION_CATALOG = DefinitionCatalog(_DEFINICIONES, CATALOG_VERSION)
