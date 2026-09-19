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
│   └── models/            # velas, indicadores, eventos, ciclos_analisis
├── domain/
│   └── symbols.py         # whitelist explícita de símbolos operables (sección 21)
├── data/
│   ├── exchange_client.py     # cliente mínimo de Binance mainnet (velas, solo lectura)
│   ├── candle_ingestion.py    # paginación + backfill idempotente hacia `velas`
│   ├── indicator_calculator.py  # RSI/ATR/SMA/volumen relativo/distancia a máximos (deterministas, puro)
│   └── indicator_pipeline.py    # orquesta el cálculo sobre muchas combinaciones + persistencia
├── repositories/
│   ├── health.py          # health check de base de datos
│   ├── candles.py         # persistencia + lectura idempotente de velas
│   └── indicators.py      # persistencia idempotente de indicadores
├── ai/, execution/, results/, orchestration/, ui/
│   # paquetes reservados por la arquitectura del plan (sección 15.1),
│   # todavía vacíos: se irán poblando fase a fase.
migrations/                # Alembic
scripts/
├── backfill_candles.py     # CLI: backfill histórico completo
└── calculate_indicators.py # CLI: cálculo masivo de indicadores
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
- Tests unitarios (configuración, validaciones del cliente de Binance,
  dispatch de `main`, lógica de paginación del backfill con una fuente
  falsa, fórmulas de indicadores contra casos calculados a mano) y de
  integración (conexión a la base de datos, idempotencia de las
  restricciones únicas de `velas` e `indicadores`, descarga real de
  velas, backfill completo incluyendo reintento sin duplicación,
  pipeline completo de cálculo de indicadores).

## Qué NO está implementado

Deliberadamente fuera de alcance de este entregable (sección 29 del master
prompt — "no sobreimplementar"):

- Actualización incremental programada (correr el backfill solo, sin un
  scheduler; APScheduler queda para cuando exista el orquestador).
- Un script formal de validación (`scripts/verify_indicators.py`) que
  automatice la comparación contra una fuente externa — por ahora la
  validación contra TradingView es manual, comando por comando.
- Fórmula del pre-filtro que combina los indicadores (la sección 6.1 del
  plan la deja explícitamente pendiente "hasta ver estos valores
  corriendo con datos reales").
- Catálogo de eventos y correlación histórica.
- Agentes de IA (interviewer, cuantitativo, eventos, auditor, consolidador).
- Motor de ejecución, órdenes, posiciones y monitoreo — ninguna credencial
  de Binance se usa todavía; ni el cliente de velas ni el backfill la
  necesitan.
- Cálculo de resultados y métricas de performance.
- Interfaz Streamlit.
- Cualquier conexión a Anthropic o Telegram.

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
