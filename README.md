# Plataforma de trading cripto — esqueleto técnico (primer entregable)

Sistema personal de análisis y paper trading en el mercado **spot** de
criptomonedas. La especificación completa vive en
`plan-plataforma-trading-cripto(editado).md` (fuera de este repo, en el
proyecto de Claude) — este README solo cubre cómo levantar y verificar la
infraestructura base.

**Importante:** esto todavía NO es el sistema de trading. Es el esqueleto de
infraestructura descrito en la sección 16 del plan: base de datos, migraciones,
configuración, logging y un health check. No hay ingesta de Binance, ni
indicadores, ni agentes de IA, ni ejecución de órdenes. Ver "Qué NO está
implementado" más abajo.

## Requisitos

- Python 3.11 o superior
- Docker y Docker Compose
- Git

## Instalación

```bash
git clone <url-del-repo>
cd crypto-trading-platform
python3 -m venv .venv
source .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Configuración

```bash
cp .env.example .env
```

Los valores por defecto de `.env.example` ya coinciden con el
`docker-compose.yml` de este proyecto (usuario `crypto`, base de datos
`crypto_trading`, puerto `5432`), así que para levantar el entorno local no
hace falta editar nada. Las claves de Binance, Anthropic y Telegram se dejan
vacías: no se usan en este entregable.

**Nunca** definas `ANTHROPIC_API_KEY` como variable de entorno del sistema
(fuera de este `.env`): si Claude Code la encuentra en el entorno, la usa
para autenticarse en vez de tu suscripción Max, generando cobros de API.
Verifica con `echo $ANTHROPIC_API_KEY` en una terminal nueva — debe salir
vacío.

## Arranque de PostgreSQL + TimescaleDB

```bash
docker compose up -d
```

Espera a que el healthcheck del contenedor esté en estado `healthy`:

```bash
docker compose ps
```

## Migraciones

```bash
alembic upgrade head
```

Esto crea la extensión `timescaledb`, las cuatro tablas iniciales (`velas`,
`indicadores`, `eventos`, `ciclos_analisis`) y convierte `velas` en
hipertabla particionada por `open_time`.

## Verificar que el entorno funciona

```bash
python -m app.main healthcheck
```

Debe imprimir `OK: Conexión OK. Extensión timescaledb instalada.` y salir
con código 0. Si falla, la causa más común es que el contenedor de Postgres
todavía no terminó de arrancar, o que `alembic upgrade head` no se ejecutó.

## Descargar velas reales (Fase 0 del plan)

```bash
python -m app.main fetch-candles BTCUSDT 1d 100
```

Descarga velas reales desde **Binance mainnet** (endpoint público de solo
lectura, sin API key) y las imprime en la terminal. Todavía no las guarda
en la base de datos — eso es el siguiente bloque (ingesta completa e
idempotente). Solo acepta símbolos de la whitelist explícita
(`app/domain/symbols.py`, sección 21 del plan) y las temporalidades `1h`,
`4h`, `12h`, `1d` (sección 6.1 del plan); cualquier otro valor falla con un
mensaje claro en vez de intentar adivinar.

No requiere Docker ni la base de datos levantada — solo salida a internet
hacia `api.binance.com`.

## Backfill histórico (ingesta persistente)

```bash
python -m scripts.backfill_candles --days 30 --symbols BTCUSDT,ETHUSDT --timeframes 1d
```

Descarga velas reales y las guarda en la tabla `velas`, paginando hacia
atrás en el tiempo hasta cubrir la ventana pedida. Es idempotente: correr
el mismo comando dos veces no duplica ninguna vela (usa `INSERT ... ON
CONFLICT DO NOTHING` sobre la restricción única `symbol+timeframe+open_time`,
sección 15.7 del plan), así que un corte a mitad de camino se resuelve
simplemente volviendo a correrlo.

Sin argumentos, `python -m scripts.backfill_candles` descarga ~2 años
(sección 3 del plan) para los 20 símbolos de la whitelist en las 4
temporalidades decididas — puede tardar varios minutos por el
rate-limiting de Binance. Usa `--days`/`--symbols`/`--timeframes` para
probar rápido con una ventana o un subconjunto más chicos primero.

Requiere Docker levantado y la migración aplicada (guarda en la base de
datos), además de salida a internet hacia `api.binance.com`.

## Cálculo de indicadores (Paso 4 del plan)

```bash
python -m app.main calculate-indicators BTCUSDT 1d
```

Lee las velas ya guardadas de `symbol`/`timeframe` (no descarga nada de
Binance — corre primero el backfill si hace falta), calcula los indicadores
deterministas de la sección 6.1 del plan y los guarda en la tabla
`indicadores`:

- RSI-14 y ATR-14 (método de Wilder), en cualquier temporalidad.
- Medias móviles SMA-90/120/180, en cualquier temporalidad.
- Volumen relativo (volumen de la vela ÷ SMA-30 de volumen), en cualquier
  temporalidad.
- Distancia al ATH y al máximo de 90 días — **solo** en velas diarias
  (`1d`), tal como fija la sección 6.1.

Imprime el último valor de cada indicador para poder compararlo a mano
contra TradingView — la Fase 1 del plan exige esa validación manual antes
de seguir avanzando ("Hecho cuando: tus números coinciden con
TradingView"). Es idempotente igual que el backfill de velas: recalcular
sobre las mismas velas no duplica ningún valor.

**Limitación conocida y documentada** (no un bug): "distancia al ATH" usa
el máximo dentro de la ventana de velas efectivamente ingerida (~2 años),
no el máximo histórico real desde que el activo empezó a cotizar — ver el
docstring de `app/data/indicator_calculator.py` para el detalle.

Requiere Docker levantado y la migración aplicada. No necesita salida a
internet (trabaja solo con lo que ya está en la base de datos).

### Cálculo masivo (los 20 símbolos históricos)

```bash
python -m scripts.calculate_indicators
```

Corre `calculate-indicators` internamente para las 80 combinaciones
(20 símbolos x 4 temporalidades), sobre las velas que ya dejó el backfill.
Como el backfill, hace `commit()` después de cada combinación — no al
final de todas —, así que un corte a mitad de camino no pierde el progreso
y volver a correrlo no duplica nada. Una combinación sin velas guardadas
todavía se reporta como "sin velas guardadas" en vez de fallar.

### Verificación de indicadores

```bash
python -m scripts.verify_indicators
```

Chequeo de integridad, de solo lectura: para cada combinación,
recalcula los indicadores a partir de las velas guardadas y compara ese
resultado contra lo que realmente quedó en la tabla `indicadores` (detecta
filas perdidas, corrompidas, o calculadas con una versión vieja del código
y nunca recalculadas), y además valida invariantes matemáticos que deben
cumplirse siempre: RSI entre 0 y 100, ATR y volumen relativo no negativos,
distancia al máximo/ATH nunca positiva. Termina con código de salida 1 si
encuentra alguna discrepancia.

**No reemplaza la validación manual contra TradingView** (la del comando
`calculate-indicators` de arriba): esta verificación es interna —
recalculado vs. persistido — y no tiene forma de saber si la fórmula en sí
coincide con una fuente externa. Esa comparación sigue siendo manual,
símbolo por símbolo, porque TradingView no ofrece una API pública estable
para automatizarla.

## Catálogo de eventos y análisis (Paso 5 del plan)

```bash
python -m scripts.seed_events              # carga/actualiza el catálogo desde data/events/*.json
python -m scripts.seed_events --dry-run    # valida el catálogo sin tocar la base de datos
python -m scripts.calculate_event_analysis # calcula analisis_eventos para todo el catálogo cargado
```

`eventos` es un catálogo **curado manualmente** (sección 3 del plan: "40-60
eventos fechados"), no generado por código — ver `data/events/README.md`
para el formato exacto de los archivos JSON de catálogo. `seed_events.py`
hace upsert por `external_id`: corregir un evento en el archivo (una fecha,
una fuente) y volver a correr el comando aplica la corrección en vez de
duplicar la fila.

`data/events/catalogo_v1.json` contiene la primera versión del catálogo:
43 eventos (2012-11-28 a 2026-09-15) investigados y verificados uno por uno
con fuente pública citada — halvings de Bitcoin, quiebras/hackeos de
exchanges (Mt. Gox, FTX, Celsius, 3AC, Ronin, Poly Network, Silvergate,
Genesis, Voyager), demandas y decisiones regulatorias (SEC vs. Ripple/
Binance/Coinbase, caso Grayscale-GBTC, CLARITY Act, GENIUS Act), adopción
institucional (MicroStrategy, Tesla, Coinbase, Reserva Estratégica de
Bitcoin de EE. UU., Cypherpunk/Zcash) y aprobaciones/elegibilidad de ETFs
spot (BTC, ETH, y el proceso en curso de ADA bajo los estándares genéricos
de listado de 2025). No fue generado por un LLM sin supervisión: cada fecha
fue confirmada por al menos una fuente antes de incluirse, y ninguna fecha o
cifra especulativa (p. ej. una fecha límite de decisión de la SEC que no es
legalmente vinculante) se incluyó como si fuera un hecho consumado. Sigue
por debajo del techo de 60 eventos del plan — queda explícitamente abierto a
ampliarse (más eventos específicos de altcoins de la whitelist, más crisis
macro) sin que eso bloquee el uso del catálogo actual.

`calculate_event_analysis` (`app/data/event_analyzer.py` +
`app/data/event_pipeline.py` + `app/repositories/event_analysis.py`)
calcula, para cada evento y cada símbolo de su `scope` (o los 20 de la
whitelist si el evento es `"GLOBAL"`), el cambio porcentual de precio entre
el día del evento y `evento ± {7, 1, 3, 7, 30}` días — a partir de velas
diarias ya guardadas, no de Binance. Un evento más antiguo que el
historial de velas ingerido (~2 años) se reporta como "sin velas" en vez de
inventar un valor. Es idempotente, igual que indicadores.

**Lo que esto NO incluye todavía**: la definición estadística de "evento
comparable" que usa el Agente 3 (analista de eventos) — la sección 17 del
plan la deja explícitamente pendiente hasta tener el catálogo real
corriendo con datos. `analisis_eventos` es el insumo para esa definición,
no la definición en sí.

## Pre-filtro determinista (Paso 6.1 del plan)

```bash
python -m scripts.inspect_prefilter_inputs  # distribución real de indicadores (solo lectura)
python -m scripts.run_prefilter             # puntúa los 20 símbolos y muestra los candidatos del día
```

`app/data/prefilter.py` (cálculo puro) + `app/data/prefilter_pipeline.py`
(lee velas/indicadores diarios reales) calculan un puntaje 0-100 por
símbolo combinando tendencia (promedio %-precio-vs-SMA 90/120/180),
volumen relativo y distancia al máximo de 90 días, con normalización
absoluta de límites fijos y pesos iguales (decisiones confirmadas con el
usuario tras ver la distribución real de datos — sección 6.1/17 del plan).
`app/domain/rules/scoring.py` fija los umbrales `SCORE_MINIMO_PREFILTRO=45`
y `MAX_CANDIDATOS_PREFILTRO=5` (sección 19). Un símbolo con cualquier dato
faltante no recibe un puntaje inventado (sección P3): se reporta aparte.
Todavía no persiste nada (`factores`/`propuestas` son del Paso 7).

## Agentes de IA (Paso 6 del plan)

```bash
python -m scripts.run_agent_demo  # llamada real al Auditor con datos de ejemplo — consume API de pago
python -m scripts.run_agent_demo_full_cycle  # los 4 agentes en secuencia con datos REALES de un símbolo — consume API de pago
```

`app/ai/client.py` (cliente delgado sobre el SDK oficial de Anthropic,
con el prompt de sistema marcado para prompt caching) + `app/ai/schemas.py`
(schemas Pydantic que reproducen exactamente el JSON de "SALIDA" de cada
prompt) + `app/ai/prompts/*.py` + `app/ai/agents/*.py` implementan los
cuatro agentes no conversacionales: Analista cuantitativo y Analista de
eventos (Sonnet 5.5), Auditor de datos (Haiku 4.5) y Consolidador (Opus
5.5). Cada llamada válida queda registrada en `llamadas_ia`
(`app/repositories/ai_calls.py`) con tokens y costo estimado. Los agentes
no reciben ninguna herramienta de ejecución ni importan nada de
`app.execution`/`app.data.exchange_client` (P2/P4 del plan). El bloque
DATOS de cada llamada lo arma quien invoca al agente — construirlo a
partir de datos reales de la base es responsabilidad del Paso 7
(orquestación), todavía no implementado.

**Consolidador (Agente 5) — diseño co-construido con el usuario el
2026-09-29**: el plan (sección 7.2) nunca llegó a especificar su JSON de
"SALIDA" (a diferencia de los otros tres) y su tabla de ponderación
contradice su propia descripción textual (una tabla aditiva de 5
categorías sumando 100% frente a una calidad de datos descrita como
multiplicador del puntaje final — dos modelos matemáticos incompatibles).
Se resolvió a favor del modelo multiplicativo, reforzando P1 ("el código
calcula, la IA interpreta"): el agente entrega solo su juicio 0-100 de
cuatro categorías (`tendencia_momentum`/`volumen_liquidez`/
`posicion_rangos_ath`/`contexto_eventos`) por opción — nunca un puntaje ya
combinado. `app/domain/rules/scoring.py::calculate_consolidated_score`
(código determinista y testeado, `tests/unit/test_scoring_consolidador.py`)
combina esas cuatro categorías con los pesos fijos 30/20/20/30, aplica la
calidad de datos del Auditor (no la juzga el Consolidador) como
multiplicador, y compara el resultado contra `SCORE_MINIMO_ENTRADA=55`
(sección 19 del plan). `app/ai/agents/consolidator.py` además verifica que
cada `fuentes` citada exista realmente en el catálogo de eventos
(`app/repositories/events.py::get_existing_external_ids`) — nunca acepta
un id inventado por el modelo (P3 del plan).

**`scripts/run_agent_demo_full_cycle.py` — los 4 agentes en secuencia con
datos reales**: elige el mejor candidato del pre-filtro de hoy (o el símbolo
que se le pase con `--simbolo`), arma el bloque DATOS de cada agente leyendo
`velas`/`indicadores`/`eventos`/`analisis_eventos` YA calculados en la base
real (nunca inventa un valor — un dato ausente se declara como faltante), y
corre los 4 agentes de verdad en su orden de dependencia. Sirve para
verificar el tubo completo con datos reales antes de comprometerse al
esquema de persistencia del Paso 7; no persiste nada en
`factores`/`propuestas`/`ciclos_analisis`. Las funciones que arman cada
bloque DATOS están testeadas contra Postgres real
(`tests/integration/test_run_agent_demo_full_cycle.py`); el propio `main()`
(4 llamadas reales y pagas a la API) no, igual que `run_agent_demo.py`.

**Diferido deliberadamente**: el Agente 1 (Entrevistador) es conversacional
multi-turno y su perfil de salida no tiene tabla asignada en el plan —
se construirá junto con Streamlit (Paso 10).

## Tests

```bash
pytest
```

Los tests de integración corren contra Postgres real, no contra un mock —
pero contra una base de datos **separada** de la que usa la aplicación
(`crypto_trading_test`, no `crypto_trading`), para que nunca lean ni
escriban sobre datos verdaderos (bug real encontrado el 2026-09-19: antes de
esto, los tests corrían contra la misma base que ya tenía velas reales
backfillizadas, y algunos empezaron a fallar por contar o chocar con esas
filas reales — ver `tests/conftest.py::_resolve_test_database_url`).

**Máquina limpia:** `docker compose up -d` ya crea ambas bases
automáticamente (ver `docker/init-test-db.sql`) — solo falta aplicar las
migraciones a las dos:

```bash
alembic upgrade head
DATABASE_URL=postgresql+psycopg://crypto:crypto@localhost:5432/crypto_trading_test alembic upgrade head
```

(en PowerShell: `$env:DATABASE_URL="postgresql+psycopg://crypto:crypto@localhost:5432/crypto_trading_test"; alembic upgrade head`
— y no olvides volver a abrir una terminal nueva o repetir `alembic upgrade head`
sin la variable después, para no dejarla pisando tu `DATABASE_URL` real en esa sesión de shell.)

**Si ya tenías el contenedor corriendo desde antes** (como es probablemente
tu caso): el script de inicialización solo corre la primera vez que se crea
el volumen, así que hay que crear la base de pruebas a mano, una sola vez:

```bash
docker exec -it crypto_trading_postgres psql -U crypto -d crypto_trading -c "CREATE DATABASE crypto_trading_test OWNER crypto;"
```

y después aplicar las migraciones a esa base con el comando de arriba.

Una vez que la base de pruebas exista y tenga las migraciones aplicadas,
`pytest` la usa automáticamente — no hace falta tocar `.env` (los tests
aislados unos de otros mediante una transacción externa que se revierte al
final de cada test, incluso si el código bajo prueba hizo `commit()`). Los
tests de integración de `exchange_client` además requieren salida a
internet hacia `api.binance.com` (sin API key).

## Estructura del proyecto

```text
app/
├── main.py              # punto de entrada (comandos healthcheck, fetch-candles)
├── config.py             # configuración tipada (pydantic-settings)
├── logging_config.py     # logging estructurado en JSON
├── db/
│   ├── base.py            # Base declarativa de SQLAlchemy
│   ├── session.py         # engine y fábrica de sesiones
│   └── models/            # velas, indicadores, eventos, analisis_eventos, ciclos_analisis
├── domain/
│   └── symbols.py         # whitelist explícita de símbolos operables (sección 21)
├── data/
│   ├── exchange_client.py     # cliente mínimo de Binance mainnet (velas, solo lectura)
│   ├── candle_ingestion.py    # paginación + backfill idempotente hacia `velas`
│   ├── indicator_calculator.py  # RSI/ATR/SMA/volumen relativo/distancia a máximos (deterministas, puro)
│   ├── indicator_pipeline.py    # orquesta el cálculo sobre muchas combinaciones + persistencia
│   ├── indicator_verification.py # recalcula vs. persistido + invariantes matemáticos
│   ├── event_analyzer.py        # % de cambio de precio en ventanas alrededor de un evento (determinista, puro)
│   └── event_pipeline.py        # orquesta el cálculo sobre todo el catálogo + persistencia
├── repositories/
│   ├── health.py          # health check de base de datos
│   ├── candles.py         # persistencia + lectura idempotente de velas
│   ├── indicators.py      # persistencia idempotente de indicadores
│   ├── events.py          # persistencia con upsert del catálogo de eventos
│   └── event_analysis.py  # persistencia idempotente de analisis_eventos
├── ai/, execution/, results/, orchestration/, ui/
│   # paquetes reservados por la arquitectura del plan (sección 15.1),
│   # todavía vacíos: se irán poblando fase a fase.
migrations/                # Alembic
scripts/
├── backfill_candles.py         # CLI: backfill histórico completo
├── calculate_indicators.py     # CLI: cálculo masivo de indicadores
├── verify_indicators.py        # CLI: verificación de integridad de indicadores
├── seed_events.py              # CLI: carga/actualiza el catálogo de eventos
└── calculate_event_analysis.py # CLI: cálculo masivo de analisis_eventos
data/
└── events/                # catálogo de eventos (*.json) — ver data/events/README.md
tests/
├── unit/                  # no requieren red ni base de datos
└── integration/           # requieren Postgres real y/o red hacia Binance
```

La estructura completa de directorios (incluyendo los paquetes aún vacíos)
sigue exactamente la sección 15.1 del plan, para no tener que reorganizar
nada cuando se implementen las fases siguientes.

## Arquitectura

Capas con dependencia estrictamente hacia abajo: `ui → orchestration/services
→ repositories → db`. `domain` no conoce Streamlit, Binance ni Anthropic.
`execution` no importa agentes de IA. `repositories` es la única capa que
conoce SQLAlchemy. Ver sección 15.2 del plan para el detalle completo de qué
puede y no puede hacer cada capa.

## Qué está implementado

- Repositorio Git, `pyproject.toml`, entorno Python 3.11+.
- Docker Compose con PostgreSQL 16 + TimescaleDB.
- Configuración tipada con `pydantic-settings`, que falla explícitamente si
  falta `DATABASE_URL`.
- Logging estructurado en JSON (sin dependencias nuevas), con protección
  explícita contra loguear secretos.
- SQLAlchemy 2.x + Alembic, con la primera migración: extensión
  `timescaledb`, tablas `velas` (hipertabla), `indicadores`, `eventos`,
  `ciclos_analisis`.
- Health check de base de datos (conexión + extensión TimescaleDB).
- Whitelist explícita de 20 símbolos (`app/domain/symbols.py`, sección 21
  del plan): las 20 principales criptomonedas por market cap con par spot
  USDT activo en Binance, excluyendo stablecoins y activos deslistados.
- Cliente mínimo de Binance mainnet de solo lectura (`app/data/exchange_client.py`,
  vía `ccxt`): descarga velas reales, sin API key, sin ejecutar órdenes.
  Comando `python -m app.main fetch-candles`.
- Ingesta histórica persistente e idempotente (`app/data/candle_ingestion.py`
  + `app/repositories/candles.py`): pagina hacia atrás en el tiempo y
  guarda en `velas` con `INSERT ... ON CONFLICT DO NOTHING`, así que un
  reintento tras un corte nunca duplica una vela. Comando
  `python -m scripts.backfill_candles`.
- Cálculo determinista de indicadores (`app/data/indicator_calculator.py`
  + `app/data/indicator_pipeline.py` + `app/repositories/indicators.py`,
  sección 6.1 del plan): RSI-14 y ATR-14 (método de Wilder),
  SMA-90/120/180, volumen relativo, y en diaria además distancia al ATH y
  al máximo de 90 días. Comando `python -m app.main calculate-indicators`
  para un símbolo/temporalidad, `python -m scripts.calculate_indicators`
  para las 80 combinaciones. SMA-90 validado a mano contra TradingView
  (BTC/USDT diario, 2026-09-19) dentro del margen esperado por comparar
  contra una vela todavía sin cerrar.
- Verificación formal de indicadores (`app/data/indicator_verification.py`,
  secciones 22/23 del plan): recalcula los indicadores de cada combinación
  a partir de las velas guardadas y los compara contra lo persistido en
  `indicadores` (detecta filas perdidas, corrompidas o calculadas con
  código viejo), más invariantes matemáticos (RSI en [0, 100], ATR y
  volumen relativo no negativos, distancia al máximo/ATH nunca positiva).
  Comando `python -m scripts.verify_indicators`. No reemplaza la
  comparación manual contra TradingView (sigue siendo manual, ver punto
  anterior) — es un chequeo de integridad distinto y complementario, de
  solo lectura.
- Catálogo de eventos y su análisis (`app/data/event_analyzer.py` +
  `app/data/event_pipeline.py` + `app/repositories/events.py` +
  `app/repositories/event_analysis.py`, sección 6/15.10 del plan, Paso 5):
  esquema (`eventos` con upsert idempotente por `external_id`,
  `analisis_eventos`), carga desde `data/events/*.json`
  (`python -m scripts.seed_events`) y cálculo del cambio porcentual de
  precio en las ventanas ±{7, 1, 3, 7, 30} días alrededor de cada evento
  (`python -m scripts.calculate_event_analysis`). El catálogo real
  (`data/events/catalogo_v1.json`, 43 eventos investigados y verificados)
  ya está cargado y calculado (636 filas en `analisis_eventos`).
- Pre-filtro determinista (`app/data/prefilter.py` +
  `app/data/prefilter_pipeline.py`, sección 6.1/17 del plan, Paso 6.1):
  puntaje 0-100 por símbolo a partir de tendencia, volumen relativo y
  distancia al máximo de 90 días, con normalización absoluta y umbrales
  fijos (`app/domain/rules/scoring.py`). Comando
  `python -m scripts.run_prefilter`.
- Agentes de IA — los cuatro no conversacionales: Analista cuantitativo,
  Analista de eventos, Auditor de datos y Consolidador (`app/ai/`, sección
  7/15.10 del plan, Paso 6): cliente Anthropic, schemas Pydantic de
  contrato, prompts y registro de auditoría en `llamadas_ia`. El
  Consolidador además calcula su puntaje ponderado en código determinista
  (`app/domain/rules/scoring.py::calculate_consolidated_score`) y valida
  que sus fuentes citadas existan en el catálogo de eventos. Ver "Agentes
  de IA (Paso 6 del plan)" más arriba.
- Tests unitarios (configuración, validaciones del cliente de Binance,
  dispatch de `main`, lógica de paginación del backfill con una fuente
  falsa, fórmulas de indicadores y de análisis de eventos contra casos
  calculados a mano, lógica de verificación contra discrepancias simuladas,
  validación del formato del catálogo de eventos) y de integración
  (conexión a la base de datos, idempotencia de las restricciones únicas de
  `velas`, `indicadores` y `analisis_eventos`, upsert de `eventos` por
  `external_id`, descarga real de velas, backfill completo incluyendo
  reintento sin duplicación, pipelines completos de indicadores y de
  análisis de eventos, verificación de indicadores contra
  corrupción/borrado real en Postgres).

## Qué NO está implementado

Deliberadamente fuera de alcance de este entregable (sección 29 del master
prompt — "no sobreimplementar"):

- Actualización incremental programada (correr el backfill solo, sin un
  scheduler; APScheduler queda para cuando exista el orquestador).
- Comparación automatizada contra TradingView u otra fuente externa —
  `scripts/verify_indicators.py` sí existe, pero verifica integridad
  interna (recalculado vs. persistido, invariantes matemáticos), no vuelve
  a comparar contra TradingView; esa parte sigue siendo manual porque no
  hay una API pública estable de TradingView para automatizarla.
- La definición estadística de "evento comparable" que usará el Agente 3 —
  sección 17 del plan: explícitamente pendiente hasta explorarla con el
  catálogo real ya cargado (Fase 2). `analisis_eventos` es el insumo, no
  esa definición.
- Agente 1 (Entrevistador, conversacional — diferido a Paso 10/Streamlit).
  Tampoco existe todavía la orquestación que arma el bloque DATOS de cada
  agente a partir de datos reales de la base, ni la persistencia de
  `propuestas` a partir de lo que devuelve el Consolidador (Paso 7).
- Motor de ejecución, órdenes, posiciones y monitoreo — ninguna credencial
  de Binance se usa todavía; ni el cliente de velas ni el backfill la
  necesitan.
- Cálculo de resultados y métricas de performance.
- Interfaz Streamlit.
- Conexión a Telegram.

## Cómo verificar que el entorno funciona (resumen)

```bash
docker compose up -d
alembic upgrade head
python -m app.main healthcheck
pytest
```

Una máquina limpia debe poder ejecutar estos cuatro comandos, en orden,
siguiendo únicamente este README, sin conocimiento previo del proyecto
(criterio de aceptación de la sección 16 del plan).
