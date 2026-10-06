"""Servicios de la interfaz: lecturas (Paso 10a) y acciones manuales (Paso 10c).

Capa `UI -> services -> repositories` (secciones 8.8 y 15.2 del plan). La
interfaz nunca consulta la base ni calcula cifras financieras: pide a estos
servicios datos ya listos para mostrar. Reglas de esta capa:

- Lecturas (`views` y módulos de vista): solo lectura. La interfaz abre
  siempre una transacción `READ ONLY` en la base (`app.services.session`), de
  modo que una escritura accidental fallaría en la base.
- Escrituras: SOLO `commands.py` (aprobar/rechazar una entrada pendiente y
  reanudar tras una pausa). Reutiliza las funciones deterministas de
  `app.execution` que ya usan los comandos de terminal; ningún módulo de
  lectura lo importa (lo verifica `tests/unit/test_architecture.py`).
- No calcula cifras nuevas: las métricas salen de las funciones puras de
  `app.domain.rules` (las mismas que usa el resto del sistema) y los números
  por operación ya vienen calculados en `trade_results` y `trade_snapshots`.
  Aquí solo se reúnen, se ordenan, se filtran y se adelgazan series largas
  para poder dibujarlas.
- Sin IA ni Streamlit.
"""
