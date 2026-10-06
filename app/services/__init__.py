"""Servicios de LECTURA para la interfaz (Paso 10a).

Capa `UI -> services -> repositories` (secciones 8.8 y 15.2 del plan). La
interfaz nunca consulta la base ni calcula cifras financieras: pide a estos
servicios datos ya listos para mostrar. Reglas de esta capa:

- Solo lectura. Además de no tener funciones de escritura, la interfaz abre
  siempre una transacción `READ ONLY` en la base (`app.services.session`), de
  modo que una escritura accidental fallaría en la base.
- No calcula cifras nuevas: las métricas salen de las funciones puras de
  `app.domain.rules` (las mismas que usa el resto del sistema) y los números
  por operación ya vienen calculados en `trade_results` y `trade_snapshots`.
  Aquí solo se reúnen, se ordenan, se filtran y se adelgazan series largas
  para poder dibujarlas.
- Sin IA ni Streamlit.
"""
