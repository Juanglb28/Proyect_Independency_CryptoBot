"""Importa todos los modelos ORM para que `Base.metadata` los conozca.

Alembic (`migrations/env.py`) importa este paquete antes de comparar el
estado de la base de datos contra los modelos. Un modelo nuevo que no se
importe aquí es invisible para el autogenerate de Alembic.
"""

from app.db.models.ai_calls import LlamadaIA
from app.db.models.analysis import CicloAnalisis
from app.db.models.candles import Vela
from app.db.models.event_analysis import AnalisisEvento
from app.db.models.events import Evento
from app.db.models.indicators import Indicador

__all__ = ["Vela", "Indicador", "Evento", "CicloAnalisis", "AnalisisEvento", "LlamadaIA"]
