"""Importa todos los modelos ORM para que `Base.metadata` los conozca.

Alembic (`migrations/env.py`) importa este paquete antes de comparar el
estado de la base de datos contra los modelos. Un modelo nuevo que no se
importe aquí es invisible para el autogenerate de Alembic.
"""

from app.db.models.ai_calls import LlamadaIA
from app.db.models.alertas import Alerta
from app.db.models.analysis import CicloAnalisis, PrefiltroResultado
from app.db.models.candles import Vela
from app.db.models.event_analysis import AnalisisEvento
from app.db.models.equity import EquityObservacion
from app.db.models.events import Evento
from app.db.models.indicators import Indicador
from app.db.models.kronos_forecasts import KronosForecast
from app.db.models.orders import Orden
from app.db.models.performance import PerformanceMetric
from app.db.models.perfil_usuario import PerfilUsuario
from app.db.models.positions import Posicion
from app.db.models.snapshots import TradeSnapshot
from app.db.models.system_events import EventoSistema
from app.db.models.trade_results import TradeResultModel

__all__ = ["Vela", "Indicador", "Evento", "CicloAnalisis", "AnalisisEvento", "LlamadaIA", "Orden", "Posicion", "EventoSistema", "EquityObservacion", "TradeSnapshot", "TradeResultModel", "PerformanceMetric", "PerfilUsuario", "PrefiltroResultado", "Alerta", "KronosForecast"]
