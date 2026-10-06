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

- Python 3.11 o superior (solo si vas a instalar el venv nativo — ver
  "Ejecutar comandos dentro de Docker" más abajo para la alternativa que
  solo necesita Docker)
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

**Windows con Smart App Control activado:** si `pip install` o cualquier
comando de este README falla con `ImportError: DLL load failed... Una
directiva de Control de aplicaciones bloqueó este archivo`, no es un
paquete roto ni una versión de Python incorrecta — es Smart App Control
bloqueando las extensiones compiladas (`.pyd`) sin firma reconocida que
`psycopg`, `pydantic-core` y `SQLAlchemy` instalan (confirmado en la
práctica: pasa con cualquiera de los tres, en un venv nuevo, sin importar
la versión de Python). Con Smart App Control en "Activado" (no
"Evaluación"), Windows no permite desactivarlo sin reinstalar el sistema.
Salta directamente a "Ejecutar comandos dentro de Docker" — evita el
problema de raíz en vez de perseguirlo paquete por paquete, porque Smart
App Control gobierna ejecutables de Windows, no lo que corre dentro de un
contenedor Linux.

## Ejecutar comandos dentro de Docker (alternativa al venv nativo)

Todo comando `python -m ...` o `pytest` de este README tiene un
equivalente que corre dentro de Linux, vía el servicio `app` de
`docker-compose.yml`, sin instalar nada en el sistema operativo del
desarrollador:

```bash
docker compose up -d                                  # levanta postgres + app (app queda inactivo, listo para usarse)
docker compose run --rm app <comando>                  # corre <comando> dentro del contenedor y lo descarta al terminar
```

Por ejemplo:

| Nativo (venv)                              | Docker                                                  |
|---------------------------------------------|----------------------------------------------------------|
| `alembic upgrade head`                      | `docker compose run --rm app alembic upgrade head`        |
| `pytest`                                    | `docker compose run --rm app pytest`                      |
| `python -m app.main healthcheck`            | `docker compose run --rm app python -m app.main healthcheck` |
| `python -m scripts.run_daily_cycle`         | `docker compose run --rm app python -m scripts.run_daily_cycle` |

El repo completo se monta como volumen dentro del contenedor (ver
Dockerfile), así que los cambios de código se reflejan sin reconstruir la
imagen — solo agregar o cambiar una dependencia en `pyproject.toml`
requiere `docker compose build` antes de volver a correr algo. Los servicios
`app` y `ui` comparten la misma imagen (`crypto-trading-platform:dev`), así que
ese único comando actualiza ambos; si cambió una dependencia, reinicia también
la interfaz con `docker compose up -d ui`.

Este camino no reemplaza el venv nativo — es una alternativa completa: no
hace falta instalar Python en el sistema ni crear un `.venv` si vas a usar
siempre `docker compose run`.

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

Esto también levanta el contenedor `app` (queda inactivo — ver "Ejecutar
comandos dentro de Docker"); si solo quieres Postgres, usa
`docker compose up -d postgres`.

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
historial de velas ingerido (~2 años), o una ventana cuya fecha todavía no
tiene ninguna vela real ingerida (evento reciente, o backfill
desactualizado — bug real encontrado el 2026-09-30 corriendo
`run_agent_demo_full_cycle.py`, el Agente 3 detectó por su cuenta que dos
ventanas distintas daban el mismo cambio porcentual), se reporta como "sin
velas"/dato faltante en vez de inventar un valor o reutilizar la vela más
reciente disponible. Es idempotente, igual que indicadores; volver a
correr `calculate_event_analysis` más adelante sí completa las ventanas
que antes estaban fuera de rango.

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

**Bug real (2026-10-02):** el saldo real de la cuenta de Anthropic bajaba
más rápido que el `cost_usd` que este sistema venía reportando. Causa:
el prompt de sistema de cada agente se manda con `cache_control: ephemeral`
(prompt caching), pero la API reporta esos tokens en DOS campos separados
de `usage` — `cache_creation_input_tokens` (escritura) y
`cache_read_input_tokens` (lectura) — que `input_tokens` NUNCA incluye.
`calculate_cost_usd` solo multiplicaba `input_tokens`/`output_tokens`, así
que el costo de cachear el prompt de sistema (la parte más grande de cada
llamada) no se cobraba NUNCA — una subestimación sistemática en TODAS las
llamadas, no un caso raro. Corregido capturando ambos campos
(`app/ai/client.py::AgentCallResult`) y cobrándolos al precio de entrada
del modelo, ajustado por el multiplicador de escritura (1.25x, verificado
contra la documentación vigente de Anthropic) o de lectura (0.1x, 0.05x
para Opus 5.5 específicamente) — ver `app/ai/models.py::calculate_cost_usd`.
Los costos ya facturados antes de este fix no se pueden recalcular
retroactivamente (nunca se guardó el desglose de tokens de cache); de acá
en adelante `cost_usd` en `llamadas_ia` debería acercarse mucho más al
consumo real que reporta la consola de Anthropic — aunque sigue siendo una
cifra de observabilidad interna, no la factura real (ver el docstring de
`app/db/models/ai_calls.py`). Las llamadas que fallan después de que la
API responde (JSON inválido o truncado por `max_tokens`) sí se persisten
con su costo real desde el fix del 2026-10-02 descrito más abajo (sección
"Control de gastos de la API de Anthropic") — en el momento en que se
escribió este párrafo todavía era una brecha conocida, ya cerrada.

**Bug real (2026-10-04) — reintento ante fallo de schema:** la corrida de
las 10am abortó entera porque el Analista de eventos agregó un campo
`fecha` en cada evento comparable que su schema estricto (`extra="forbid"`)
no permite. Las fechas eran reales (del catálogo), no inventadas: fue una
desviación puntual del modelo, ya que las dos corridas anteriores pasaron
con el mismo prompt. Como el ciclo abortó, ese día no hubo propuestas y se
perdió el trabajo ya pagado de los demás candidatos. Decisión del usuario:
`run_json_agent` reintenta UNA vez (`MAX_REINTENTOS_POR_FALLO_DE_SCHEMA` en
`app/ai/agents/_runner.py`) cuando la respuesta no cumple el schema
(`AgentSchemaError`, subtipo de `AgentValidationError`). Cada intento
fallido deja su propia fila de costo en `llamadas_ia`, y
`AgentRunResult.cost_usd` suma todos los intentos (lo realmente facturado);
`AgentRunResult.intentos` dice cuántos hizo falta. **No se reintenta**: una
respuesta cortada por `max_tokens` (se repetiría igual), JSON inválido (no
es un problema de schema) ni un fallo de `extra_validate` (p. ej. el
Consolidador citando una fuente que no existe: es una señal de integridad de
datos, no ruido de formato). Se aplica a los cuatro agentes porque todos
pasan por `run_json_agent`. Es un parámetro de costo fijo en código, no una
variable de entorno.

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
datos reales, UN símbolo, sin persistir**: elige el mejor candidato del
pre-filtro de hoy (o el símbolo que se le pase con `--simbolo`) y corre los
4 agentes de verdad sobre él, usando la misma lógica de armado de DATOS que
`app/orchestration/candidate_pipeline.py` (ver más abajo). Sigue sin
persistir nada en `factores`/`propuestas`/`ciclos_analisis` — para eso está
`scripts/run_daily_cycle.py`. Útil para inspeccionar la salida cruda de un
agente sobre un símbolo puntual sin gastar en los otros candidatos del día.

**Implementado en el Paso 10b (ver "Interfaz local")**: el Agente 1 (Entrevistador) es conversacional
multi-turno; su perfil se guarda en la tabla nueva `perfil_usuario` (el plan no
le asignaba una) y es solo informativo.

## Control de gastos de la API de Anthropic

`scripts/run_daily_cycle.py` imprime, al final de cada corrida, tres cifras:

1. **Costo de esta corrida** (`cost_usd` del ciclo) — ya existía.
2. **Gasto acumulado** desde una fecha de referencia — suma real de
   `llamadas_ia.cost_usd` (`get_gasto_acumulado_desde`,
   `app/repositories/ai_calls.py`), reconstruible en cualquier momento a
   partir de los datos ya persistidos, igual que `performance_metrics`
   (sección 13 del plan).
3. **Saldo estimado restante** = saldo declarado − gasto acumulado desde
   esa fecha.

Las primeras dos cifras son exactas (dentro de lo que este sistema puede
medir — ver el bug real de abajo). La tercera es una ESTIMACIÓN, nunca el
número exacto de la consola de Anthropic: la API de Anthropic no expone
ningún endpoint de saldo real, así que el "saldo declarado" lo escribís vos
mismo en `.env` a partir de lo que ves en Billing -> Credit balance:

```bash
ANTHROPIC_SALDO_DECLARADO_USD=2.26
ANTHROPIC_SALDO_DECLARADO_EN=2026-10-02T00:00:00+00:00
```

Si no se definen, el reporte simplemente omite el saldo restante estimado
(no inventa un número — sección P3 del plan). Recalibrá ambos valores cada
vez que revises el saldo real en la consola, para que la estimación no se
aleje demasiado.

**Por qué es una estimación y no la cifra exacta**, aun después del fix de
cache y del fix de llamadas fallidas (ambos abajo): no incluye gasto de
errores que ocurren ANTES de recibir cualquier respuesta de la API (caída
de red, error de autenticación, rate limit — casos que normalmente
tampoco cobra Anthropic, ver `_persistir_llamada_fallida` más abajo) ni
ningún gasto fuera de este sistema que comparta la misma cuenta/API key.

**Bug real (2026-10-02):** el usuario notó que el saldo real de su cuenta
bajaba más rápido que el `cost_usd` que este sistema reportaba —
compró 5 USD, el saldo mostraba 2.26 USD restantes, pero el sistema solo
tenía registrados 1.34 USD de costo. Causa encontrada: el prompt de
sistema de cada agente se manda con `cache_control: ephemeral` (prompt
caching), pero la API reporta esos tokens en dos campos separados de
`usage` — `cache_creation_input_tokens` (escritura, 1.25x el precio de
entrada) y `cache_read_input_tokens` (lectura, 0.1x, 0.05x para Opus 5.5) —
que `input_tokens` NUNCA incluye. `calculate_cost_usd` solo multiplicaba
`input_tokens`/`output_tokens`, así que el costo de cachear el prompt de
sistema — la parte más grande de cada llamada — no se cobraba nunca: una
subestimación sistemática en TODAS las llamadas, no un caso raro.
Corregido en `app/ai/client.py::AgentCallResult` y
`app/ai/models.py::calculate_cost_usd` (multiplicadores verificados contra
`docs.claude.com` el 2026-10-02). Los costos ya facturados antes de este
fix no se pueden recalcular retroactivamente — nunca se guardó el
desglose de tokens de cache de esas llamadas. Separado de este bug: la
consola de Anthropic también mostraba `$1.34` bajo "Spend limits" con la
leyenda "Resets Nov 1, 2026" — ese contador es del mes calendario en
curso, no del total gastado desde la compra; si hubo gasto de pruebas
antes del 1 de octubre, ese gasto ya había bajado el saldo pero no
aparece en ese contador mensual — una fuente de confusión aparte del bug
real, no algo que el código de este proyecto pueda corregir.

**Bug real (2026-10-02, segundo hallazgo):** `llamadas_ia.respuesta` era
`NOT NULL`, así que una llamada que fallaba DESPUÉS de que la API ya
respondiera — JSON inválido, truncada por `max_tokens`
(`AgentResponseError`), o con forma distinta al schema del agente
(`AgentValidationError`, incluida la validación de que una "fuente" citada
por el Consolidador exista en el catálogo de eventos) — nunca se
persistía: no había ninguna fila que guardar con `respuesta=None`. Pero
Anthropic cobra esa llamada igual (generó tokens de salida hasta el corte,
o un JSON completo que simplemente no sirvió), así que ese costo quedaba
invisible para "gasto acumulado" de arriba — exactamente la brecha que
este mismo README documentaba como pendiente hasta ahora. Corregido con
una migración (`858d5d2840d7`) que agrega `exitosa: bool` (default
retroactivo `true` — toda fila previa a esta migración es, por
definición, una llamada que sí se persistió con éxito) y `error_message:
str | None`, y vuelve `respuesta` nullable. `app/ai/agents/_runner.py`
ahora envuelve la llamada/parseo/validación en un
`try/except (AgentResponseError, AgentValidationError)` que llama a
`_persistir_llamada_fallida` (guarda `exitosa=False`, `respuesta=None`,
`error_message=str(exc)` y el costo real) antes de relanzar la misma
excepción — el comportamiento de quien orquesta el ciclo no cambia en
nada, solo deja de perderse el registro de costo. Los tokens reales nunca
se inventan (P3 del plan): vienen de `AgentResponseError.tokens` cuando
`call_agent` detecta el corte por `max_tokens` antes de devolver nada, o
del `AgentCallResult` ya recibido cuando la falla es posterior (JSON
inválido o schema incorrecto); si ninguno está disponible (un error antes
de recibir cualquier respuesta de la API), no se persiste nada, porque no
hay tokens reales que registrar.

## Segunda ronda: entradas "de mayor riesgo" (decisión del usuario, 2026-10-04)

**Por qué existe.** El ciclo del 2026-10-03 no produjo ninguna propuesta
sobre el umbral de entrada (55): los mejores puntajes ponderados fueron
LTC 49.80, AVAX 47.80 y ETH 46.30. El usuario, mirando LTC (momentum
fuerte, RSI alto también contra BTC), consideró defendible una entrada de
mayor riesgo y propuso un criterio adicional para esos casos. **No se bajó
el umbral global ni se tocaron los pesos 30/20/20/30**: la v1 existe para
medir si el score predice (sección 1 del master prompt), y ajustar el
umbral o los pesos después de ver un caso concreto contaminaría esa
validación. En su lugar se agregó un tier SEPARADO y ETIQUETADO, para poder
comparar después si estas entradas rinden mejor o peor que las normales.

**Cómo funciona** (todo en código, ningún LLM interviene — P1;
`app/domain/rules/entry_tier.py`). Solo se evalúa una propuesta cuyo
`puntaje_ponderado` esté en `[50, 55)`. Para calificar como
`TierEntrada.MAYOR_RIESGO` debe cumplir las tres verificaciones:

1. **Fuerza relativa vs BTC:** RSI 14 diario del ratio `SÍMBOLO/BTCUSDT` > 50.
2. **Momentum multitemporal:** RSI 14 > 50 en al menos 3 de las 4
   temporalidades (1h, 4h, 12h, 1d).
3. **Tope de sobrecompra:** RSI 14 diario <= 70.

Todos los umbrales son estrictos donde dice ">" (un RSI exactamente en 50
no cuenta) y están probados en su valor exacto
(`tests/unit/test_entry_tier.py`). El resto de propuestas queda como
`NORMAL` (supera 55) o `NINGUNO`.

**El ratio contra BTC se calcula, no se descarga:** `app/data/
relative_strength.py` divide los cierres de las velas USDT que ya existen
(`LTCUSDT / BTCUSDT`) y aplica el mismo RSI de Wilder del resto del
proyecto. No se ingesta ningún par nuevo (p. ej. LTCBTC) ni se amplía la
lista de símbolos autorizados. Es una aproximación del par cruzado real de
Binance, que cotiza por su cuenta y puede diferir levemente.

**Datos faltantes nunca se suponen (P3):** si falta un RSI o no hay
historia suficiente para el ratio, la verificación que dependa de ello
falla y el dato queda listado en `faltantes`. **BTCUSDT nunca puede
calificar por esta vía:** su ratio contra sí mismo es constante y no define
una fuerza relativa; se declara como faltante en vez de saltarse el
criterio con una regla inventada.

**Trazabilidad.** `propuestas` ganó dos columnas (migración `c0855d96f99b`):
`tier_entrada` y `segunda_ronda` (JSONB con el valor y el veredicto de cada
verificación). Ambas quedan también congeladas en el `decision_snapshot`.
Son NULLABLE y sin backfill a propósito: las propuestas anteriores nacieron
sin este criterio y rellenarlas sería reescribir historia congelada
(sección 14 del plan). En la salida de `scripts/run_daily_cycle.py` una
propuesta de este tier aparece marcada como `ENTRADA DE MAYOR RIESGO`.

**Observación de persistencia (2026-10-04, solo registro).** El usuario
planteó que un activo que queda varios días seguidos cerca del umbral es un
patrón: "algo está a punto de suceder". Por ahora se guarda como
OBSERVACIÓN, sin que decida ninguna entrada: con unos pocos días de
historia no hay forma de validar la hipótesis, y volverla criterio ya sería
el mismo ajuste de reglas mirando casos sueltos que se evitó al no bajar el
umbral. Cada propuesta guarda `ciclos_consecutivos_en_zona` (migración
`de1706e3afa6`): cuántos días de análisis seguidos, contando el actual, lleva
el activo con puntaje en `[50, 55)` (`contar_ciclos_consecutivos_en_zona`,
`app/domain/rules/entry_tier.py`; 0 si hoy no está en la zona). Reglas del
conteo: un día con puntaje en la zona prolonga la racha; uno fuera de la
zona la corta; **un día de análisis exitoso donde el activo no tuvo
propuesta también la corta** (no se sabe si estuvo cerca, y suponerlo sería
inventar un dato — recordar que `propuestas` solo guarda lo que el
Consolidador devuelve, p. ej. 3 de 5 candidatos el 2026-10-03). Cuenta
días de análisis exitosos, no días de calendario: un ciclo fallido o un día
sin ejecución no corta la racha. Se agrupa por día UTC (varios ciclos el
mismo día cuentan una vez, con el mayor puntaje). Cuando exista el módulo
de resultados (Paso 9) se podrá comparar el desempeño de las entradas por
días de racha y decidir si se promueve a criterio. Una racha larga NO
cambia el tier (probado en `tests/integration/test_daily_cycle.py`).

**Pendiente — Paso 8 (decisión del usuario, aún sin implementar porque no
existe el motor de ejecución):** toda entrada de este tier requerirá
APROBACIÓN MANUAL SIEMPRE y usará el tamaño mínimo ya existente (100 USD),
sin introducir ningún parámetro de dinero nuevo. Hoy este tier solo
clasifica y registra; no abre ninguna posición.

**Nota de calibración.** El corte inferior en 50 fue mantenido a propósito
por el usuario aun sabiendo que LTC (49.80) habría quedado fuera ese día:
se prefirió no ajustar el umbral mirando un solo caso. Con los datos del
2026-10-03, ninguna propuesta habría calificado.

## Decisiones del Paso 8 (motor de ejecución paper) — cerradas el 2026-10-04

Decididas por el usuario antes de escribir una sola línea de `risk.py`
(master prompt, sección 30: nada que afecte dinero, riesgo o ejecución se
inventa). Todas se suman a las reglas fijas ya cerradas en la sección 9 del
plan (stop -8%, TP 50% en +12%, trailing 6%, pausa en -15%, máx. 4
posiciones, 100-250 USD, aprobación manual sobre 200 USD, score mínimo 55,
3 operaciones por día, `client_order_id`, archivo `PARAR`, monitor cada
60 s). Ya existen las reglas puras (`risk.py`, `execution.py`), las tablas y
sus repositorios, el servicio de entradas y, desde el 2026-10-05, el
**monitor de 60 s con los cierres, el equity, la pausa automática por
drawdown, la reconciliación y `PARAR`** (ver "Monitor, cierres, equity y
pausa", más abajo).

| Tema | Decisión |
|---|---|
| Tamaño por posición | **Fijo 100 USD** para todas las entradas (mínimo del plan). Mantiene el tamaño fuera de la validación del score; las entradas NORMAL quedan bajo el umbral de 200 USD y se ejecutan solas. |
| Precio de salida si se cruza un nivel | **Precio observado** en el chequeo (no el precio exacto del nivel). Si el precio salta de -7% a -9%, se cierra a -9%: el paper no infla resultados. Aplica por igual al stop, al TP parcial y al trailing (extensión coherente de la misma regla). |
| Costos del paper | **Comisión 0,1% por lado**; `estimated_*_slippage` queda **NULL** (campo nullable en el plan) y se trata como 0 en `net_pnl`, documentado. Sin libro de órdenes real, un porcentaje sería un número inventado (P3). |
| Base del -15% | **Pico de equity** (capital + P&L realizado y no realizado), la misma definición de Maximum Drawdown de la sección 8.6. |
| Pausa total (-15%) | **Solo bloquea entradas nuevas.** Las posiciones abiertas siguen vigiladas con stop/TP/trailing. Reanudar es siempre una acción manual del usuario, nunca automática. |
| Selección de propuestas | Se ordenan por puntaje ponderado (mayor a menor) hasta llenar los cupos (4 abiertas, 3 por día). Nunca se abre un símbolo que ya tiene posición abierta. Las NORMAL entran antes que las MAYOR_RIESGO. |
| Tope de MAYOR_RIESGO | Comparten los cupos generales **y además** tienen un tope propio de **1 posición abierta a la vez**. Siempre aprobación manual y 100 USD (ya decidido el 2026-10-04). |
| Aprobación manual | Estado `PENDING_APPROVAL`; se aprueba o rechaza con un **comando de terminal**; Telegram/Streamlit reutilizarán el mismo servicio. **Vence al terminar el día UTC del ciclo** (00:00 UTC del día siguiente; "vence fin del día") si no se resuelve: el análisis envejece. |
| Salida por tiempo | **Ninguna** (fiel al plan). Solo stop, TP con trailing y pausa por drawdown. Si posiciones estancadas retrasan las 30 operaciones, se mide y se decide con datos. |
| Fuente de precio del monitor | **Ticker público de Binance** (endpoint de mercado sin credenciales). El monitor no necesita API key: ninguna credencial de exchange se usa para vigilar posiciones paper. |
| Precio base de stop y objetivo | **Precio de ejecución**: se calculan sobre el precio real al que se abre la posición paper (`calculate_initial_levels(entry_price)`); si la entrada esperó aprobación, el del momento de aprobarla. El `decision_snapshot` conserva el precio del análisis sin tocarlo. Los niveles quedan en `posiciones.initial_*`. |
| Entradas rechazadas o expiradas | **Devuelven todo lo que reservaban**: cupo de posición, tope de mayor riesgo y la entrada del día. Por eso la "entrada del día" cuenta solo órdenes `ENTRY` en `PENDING_APPROVAL` o `FILLED`. |
| Estado de la pausa por drawdown | Tabla nueva `eventos_sistema`, **solo-agregar** (un trigger rechaza `UPDATE`/`DELETE`). Estado actual = tipo del último evento. **Reanudar fija un pico de equity nuevo igual al equity de ese momento** (si no, `equity <= pico × 0,85` volvería a pausar al instante) y guarda el pico anterior como registro. |
| Entradas pendientes de aprobación | **Reservan cupo**: una orden `PENDING_APPROVAL` cuenta para las 4 posiciones, las 3 entradas del día y, si es de mayor riesgo, el tope de 1, hasta que se apruebe, rechace o expire. Evita que una entrada aprobada tarde rompa los topes. |
| Cálculo del equity (2026-10-05) | **1.000 USD de capital + P&L neto realizado (comisiones incluidas) + P&L no realizado de las posiciones abiertas al último precio observado.** Se calcula como caja + valor de mercado (la comisión de entrada de una posición abierta se descuenta desde que se paga). Si falta el precio de alguna posición abierta, ese chequeo NO guarda equity: no se inventa un precio. |
| Dónde se guarda el pico de equity (2026-10-05) | Tabla nueva `equity_observaciones`, **solo-agregar**: una fila por chequeo del monitor con equity y pico vigentes (~1.440 filas/día). El pico vigente es el de la última fila; sin filas, el capital. Sirve además de curva de equity para el Paso 9. |
| Aprobar durante la pausa (2026-10-05) | **Bloqueado** (rechazar sí se permite). Confirmado por el usuario. |
| Arranque con posiciones abiertas (2026-10-05) | El primer chequeo tras un reinicio revisa las posiciones con el **precio actual** y aplica las reglas de salida de inmediato; **nunca se cierra nada "por reinicio"**. |
| Archivo `PARAR` (derivado del plan) | Bloquea solo las operaciones **nuevas** (abrir y aprobar entradas), como la pausa. El monitor sigue vigilando y ejecutando stops: el plan dice que "si Juan no responde, el stop sigue activo". |
| Reconciliación al arrancar (derivado del plan) | En paper no hay exchange contra el cual comparar, así que verifica que `posiciones` y `ordenes` cuenten la misma historia. Ante cualquier discrepancia el monitor **se detiene sin actuar** (el plan: "notificar, bloquear operación hasta resolver"). |
| Desempate de la selección | Orden: tier (NORMAL antes que MAYOR_RIESGO) → puntaje ponderado descendente → símbolo alfabético → id de propuesta. El plan no define el desempate; hace falta uno para que el resultado sea reproducible (ciclo y backtest). Solo es determinismo, no una preferencia por un activo; fácil de cambiar. |

**Derivado del plan, sin pregunta aparte (corregir si no es lo que quieres):**
el trailing del 6% se mide desde el **precio máximo observado desde que se
activó** (al tocar +12%), actualizado en cada chequeo y nunca a la baja. Su
nivel (≥ +5,3% sobre la entrada) siempre queda por encima del stop de -8%,
así que el stop original queda superado de forma automática.

**Cerrado después (2026-10-04):** una entrada en `PENDING_APPROVAL` SÍ
reserva cupo, y si se rechaza o expira lo devuelve todo (ver la tabla). El
servicio que arme `SituacionDeCupos` debe contar posiciones abiertas +
entradas pendientes, y las entradas del día como las órdenes `ENTRY` en
`PENDING_APPROVAL` o `FILLED` creadas en el día UTC actual
(`count_entry_orders` con esos estados).

**Tablas creadas (migración `f3cbb4cdb7d1`, 2026-10-04):** `posiciones` y
`ordenes`, con el diseño aprobado por el usuario (modelos en
`app/db/models/positions.py` y `orders.py`; enums `LadoOrden`,
`IntencionOrden`, `EstadoOrden`, `EstadoPosicion` en
`app/domain/enums.py`). Son tablas nuevas y vacías: no tocan datos
existentes. Lo que garantiza la base de datos por sí sola (P5), cubierto por
`tests/integration/test_orders_positions_schema.py`:

- Un símbolo no puede tener dos posiciones `OPEN` a la vez (índice único
  parcial `uq_posiciones_symbol_abierta`).
- Una propuesta abre a lo sumo una posición, y tiene a lo sumo una orden
  `ENTRY` (índice único parcial `uq_ordenes_entrada_por_propuesta`), aunque
  un reintento cambie el `client_order_id`, que además es único.
- Coherencia estado/cantidad: `OPEN` siempre con cantidad restante > 0;
  `CLOSED` siempre con 0 y `closed_at`; una orden `FILLED` siempre trae
  cantidad, precio de ejecución, comisión y fecha de ejecución; trailing
  activo exige su precio máximo de referencia.
- Dinero y precios en `NUMERIC`, fechas en `TIMESTAMPTZ`, enums como
  `VARCHAR` validado (agregar un estado no requiere `ALTER TYPE`).

Los cupos (4 abiertas, 3 por día, 1 de mayor riesgo) se calculan
consultando estas tablas, sin contadores aparte. Para aplicarla: `docker compose run --rm app alembic upgrade head` (y lo mismo
sobre la base `_test`, ver "Tests").

**Reglas de riesgo en código (`app/domain/rules/risk.py`, 2026-10-04):**
funciones puras sobre `Decimal`, sin base de datos, Binance, LLM ni
Streamlit, con las constantes del plan en código versionado (nunca en
`.env`). Qué hace cada una:

- `calculate_initial_levels`: stop -8% y objetivo +12% desde la entrada,
  redondeados a la escala de la columna (`NUMERIC(30,12)`), así lo calculado
  es exactamente lo guardado.
- `calculate_entry_quantity` y `calculate_fee`: cantidad hacia abajo (nunca
  se compra de más) y comisión de 0,1% aparte, como en las fórmulas de la
  sección 8.4 del plan.
- `evaluate_position`: dado el estado de una posición y un precio observado,
  decide `MANTENER` / `CERRAR_PARCIAL` (50% en +12%, activa el trailing) /
  `CERRAR_TOTAL` (`STOP_LOSS` o `TRAILING_STOP`) y devuelve el estado de
  trailing a persistir. Un nivel se considera alcanzado en la igualdad
  exacta. Un estado incoherente lanza `ValueError`, nunca se adivina.
- `is_drawdown_limit_reached` / `calculate_drawdown_pct`: -15% contra el
  pico de equity, comparando `equity <= pico × 85%` sin dividir.
- `validate_entry`: aplica TODAS las reglas de entrada (sistema pausado,
  whitelist, tier, símbolo ya abierto, 4 posiciones, 3 por día, tope de 1
  de mayor riesgo, rango 100-250 USD) y devuelve todos los motivos de
  rechazo, no solo el primero; indica si exige aprobación manual (mayor
  riesgo o monto > 200 USD).

Tests en `tests/unit/test_risk_rules.py`: cada umbral se prueba exacto, un
paso antes y un paso después (stop -8%, TP +12%, trailing 6%, drawdown
-15%). `Settings.max_open_positions` (variable heredada del plan) no es la
fuente de verdad: lo es `MAX_POSICIONES_ABIERTAS`, y un test falla si se
desalinean. Sigue marcada `TODO — DECISIÓN PENDIENTE` la base del tope de
25% del capital (irrelevante con el tamaño fijo de 100 USD). Los porcentajes
de stop y objetivo están cerrados: se mantienen -8% / +12% (ver el estudio
más abajo).

**Selección de propuestas (`app/domain/rules/execution.py`, 2026-10-04):**
`select_entries(candidatos, cupos)` es una función pura: ordena los
candidatos (tier → puntaje → símbolo → id), valida cada uno con
`validate_entry` contra los cupos que van quedando y devuelve
`seleccionadas` (con si requieren aprobación manual y su monto) y
`descartadas` (con todos sus motivos). Cada entrada seleccionada consume
cupo para las siguientes — se ejecute sola o pida aprobación —; las
rechazadas no consumen nada. No toca la base de datos ni crea órdenes.
Tests: `tests/unit/test_execution_selection.py`.

**Repositorios (`app/repositories/orders.py` y `positions.py`):** solo
persistencia, sin reglas de negocio. `create_orden` y `create_posicion`
son idempotentes (`ON CONFLICT` por `client_order_id` y por `proposal_id`):
un reintento devuelve lo que ya existía, nunca duplica; un choque con otra
restricción (segunda entrada de una propuesta con otro `client_order_id`,
segunda posición abierta del mismo símbolo) NO se silencia y lanza
`IntegrityError`. `resolve_pending_order` solo transiciona órdenes que
sigan en `PENDING_APPROVAL` (la segunda resolución devuelve `False`) y
`update_open_position` solo modifica posiciones `OPEN` y no puede alterar
precio de entrada, cantidad inicial, stop ni objetivo originales. Los
conteos (`count_entry_orders`, `count_open_posiciones`) reciben los estados
y la ventana ya decididos por quien llama. Tests:
`tests/integration/test_orders_positions_repositories.py`.

**Servicio de entradas (`app/execution/order_manager.py`, 2026-10-04):**
une las reglas puras, los repositorios y la fuente de precios. **No calcula
ninguna regla financiera y no importa nada de IA** (un test de arquitectura lo
verifica). Qué hace:

- `open_entries`: libera primero las aprobaciones vencidas, arma los cupos
  (`build_cupos`, contando las entradas pendientes como reservadas), elige con
  `select_entries` y, para cada entrada elegida, la ejecuta en paper al precio
  observado (crea la posición y la orden `FILLED` en la MISMA transacción) o,
  si es de mayor riesgo, crea una orden `PENDING_APPROVAL` que vence al
  terminar el día UTC del ciclo.
- `approve_pending_entry`, `reject_pending_entry`, `expire_pending_entries`:
  resuelven las pendientes. Aprobar ejecuta al precio del momento de aprobar;
  si ya venció, expira en vez de ejecutar. Rechazar o expirar devuelve todos
  los cupos que reservaba, porque simplemente deja de contar.
- Idempotencia: el `client_order_id` de una entrada es `entry-<id propuesta>`;
  una propuesta tiene a lo sumo UNA oportunidad de entrada (si se rechazó o
  expiró, no se reintenta).
- Sin precio no hay orden: si Binance no entrega un precio válido
  (`PriceUnavailable`) no se escribe nada.
- Ningún `commit` en el servicio: lo hace quien llama, así que si algo falla
  a mitad, posición y orden se revierten juntas.
- Fuente de precio (`app/execution/price_source.py`): ticker público de
  Binance, sin credenciales, solo símbolos de la whitelist, nunca un precio
  inventado ni viejo.

**Confirmado por el usuario (2026-10-05):** aprobar una entrada
mientras el sistema está pausado por drawdown se **bloquea**. La pausa
significa "no se abren entradas nuevas" y aprobar es abrir una, aunque su cupo
se reservara antes de la pausa. Rechazarla sigue permitido.

**Comandos de terminal (`scripts/entries.py`):** son la interfaz manual del
servicio, una transacción por comando. Mueven solo dinero simulado.

```
docker compose run --rm app python -m scripts.entries open --ciclo-id <UUID>
docker compose run --rm app python -m scripts.entries list
docker compose run --rm app python -m scripts.entries approve <ORDEN_ID>
docker compose run --rm app python -m scripts.entries reject <ORDEN_ID> --motivo "texto"
docker compose run --rm app python -m scripts.entries expire
```

`open` toma las propuestas `NORMAL` y `MAYOR_RIESGO` del ciclo indicado. Por
ahora es un paso manual, separado del ciclo diario: encadenarlos se decide
más adelante. Tests: `tests/integration/test_order_manager.py`,
`tests/integration/test_entries_script.py`,
`tests/unit/test_price_source.py` y `tests/unit/test_architecture.py`.

**Pausa por drawdown (`eventos_sistema`, migración `ae3d54f68225`,
2026-10-04):** tabla nueva y vacía, solo-agregar. Modelo en
`app/db/models/system_events.py` (archivo añadido a la estructura del
master prompt porque la tabla no existía en el plan), repositorio en
`app/repositories/system_events.py` y enum `TipoEventoSistema`
(`PAUSA_DRAWDOWN`, `REANUDACION`). `risk.sistema_pausado(ultimo_evento)` es
la única regla: pausado si el último evento es una pausa. El repositorio
solo acepta transiciones válidas (pausa si no hay eventos o el último es una
reanudación; reanudación solo tras una pausa): repetir un evento no escribe
nada y devuelve `False`. La base de datos exige que una reanudación traiga el
pico anterior y que su pico nuevo sea igual al equity. Un tip para entender
la regla: con 100 USD fijos y stop -8%, cada pérdida cuesta unos 8,2 USD con
comisiones, así que llegar a -150 USD requiere unas 18 pérdidas netas de las
ganancias — es un freno lento, que detecta un sistema que en conjunto no
funciona, no un mal día. Tests: `tests/integration/test_system_events.py`.
Para aplicarla: `docker compose run --rm app alembic upgrade head` (y lo
mismo sobre la base `_test`, ver "Tests").

**Stop y objetivo: se mantienen -8% / +12% (decisión del usuario,
2026-10-04, tras el estudio).** El usuario había propuesto -6% / +8%; antes
de tocar una regla que afecta dinero se midió con las velas históricas
guardadas (`python -m scripts.study_stop_target`; en Docker
`docker compose run --rm app python -m scripts.study_stop_target`). El
estudio lee las velas 1h de las 20 monedas, entra a ciegas una vez al día
(746 entradas por moneda, 14.920 en total) y cuenta qué barrera toca primero
el precio (módulo puro `app/data/barrier_study.py`, solo lectura). Es
**incondicional** (no usa el score), no simula comisiones, cierre parcial ni
trailing, y las entradas de distintas monedas están correlacionadas.
Resultado, todas las monedas juntas:

| Stop / objetivo | Toca objetivo primero | Mínimo para no perder | Margen | Mediana hasta resolver |
|---|---|---|---|---|
| -6% / +8% | 45,4% | 44,3% | +1,1 | 2,7 días |
| -6% / +12% | 35,6% | 34,4% | +1,2 | 3,6 días |
| -8% / +8% | 52,9% | 51,2% | +1,7 | 3,5 días |
| **-8% / +12% (plan, vigente)** | 42,9% | 41,0% | +1,9 | 5,0 días |

Lectura (descriptiva, no una prueba estadística):

- Los cuatro pares superan el mínimo por apenas 1-2 puntos entrando a
  ciegas (≈ +0,2% a +0,4% por operación). Con 30 operaciones el error típico
  es de 1,3-1,8 puntos, mucho mayor que las diferencias entre pares: los
  datos no distinguen un par mejor.
- Parte de ese margen viene de BTC, BNB y TRX, que subieron en la ventana
  cargada; con -6% / +8% unas 10 de 20 monedas quedan bajo el mínimo. No es
  una ventaja que dar por sentada.
- **Para qué sirve: es el listón (benchmark) que el score debe superar.** Si
  las propuestas del sistema no rinden mejor que entrar a ciegas con el mismo
  stop y objetivo, el score no aporta. Este estudio se puede repetir para
  comparar en la validación del Paso 9.
- Con stop -6% el 54% de las entradas a ciegas tocan el stop primero (46% con
  -8% / +8%): un stop corto se activa más por ruido normal de precio.
- La mediana hasta el primer toque varía de 1,1 días (ZEC) a 12,6 (TRX, con 95
  entradas sin resolver) a -6% / +8%: un porcentaje fijo se comporta muy
  distinto según la moneda. Un stop ligado a la volatilidad (ATR) sería un
  cambio del plan; se evalúa con datos de la v1, no antes.

Las constantes de `risk.py` no cambian (un test las fija en 8% y 12%). Se
mantienen durante toda la muestra de la v1: cambiarlas a mitad volvería las
operaciones incomparables. Tests: `tests/unit/test_barrier_study.py`.

Sigue pendiente, y depende de datos reales (no del Paso 8): fórmula
definitiva del pre-filtro, evento comparable, ajuste final del scoring,
ventanas del backtest, calibración numérica para capital real y si la racha
en zona pasa de observación a criterio.

## Monitor, cierres, equity y pausa (Paso 8, 2026-10-05)

Todo sigue siendo dinero SIMULADO: no se usa ninguna credencial de Binance
(solo el ticker público de precios) y nada de esto importa IA (un test de
arquitectura lo verifica).

**Qué hace un chequeo (`app/execution/monitor.py::run_monitor_tick`)**, en una
sola transacción:

1. toma el candado del equity (un solo monitor o reanudación a la vez);
2. **reconcilia** el estado (`app/execution/reconciliation.py`); si algo no
   cuadra lanza `ReconciliationBlocked` y no toca nada;
3. expira las entradas pendientes vencidas (devuelve sus cupos);
4. para cada posición abierta pide el precio y aplica las reglas
   (`app/execution/position_manager.py`): stop -8% → cierre total;
   +12% → vende el 50% y activa el trailing; trailing 6% desde el máximo →
   cierra el resto. La orden de salida (`FILLED`, al precio **observado**) y
   el cambio de la posición se escriben juntos o no se escribe nada; el
   `client_order_id` es `exit-<posición>-<intención>`, así que un reintento no
   vende dos veces. Cada posición va en su propio savepoint: el error de una no
   deshace las demás;
5. si TODAS las posiciones tuvieron precio y ninguna falló: calcula el equity,
   guarda una fila en `equity_observaciones` con el pico vigente y, si el
   drawdown llegó a -15% (la igualdad exacta ya pausa), registra la pausa en
   `eventos_sistema`. Si falta un precio o hubo un error, ese chequeo NO
   guarda equity.

**Sin IA y sin comprar nada:** la pausa solo bloquea entradas nuevas; las
posiciones abiertas siguen vigiladas y sus stops activos, también con `PARAR`.

**Comandos de terminal** (cada uno es una transacción):

```
docker compose run --rm app python -m scripts.monitor              # un chequeo
docker compose run --rm app python -m scripts.monitor --loop       # cada 60 s hasta Ctrl+C
docker compose run --rm app python -m scripts.system_control status
docker compose run --rm app python -m scripts.system_control resume
```

El monitor solo vigila **mientras corre y el PC está encendido** (limitación
de correr en local): si estuvo apagado, el primer chequeo al volver revisa las
posiciones con el precio de ese momento y aplica las reglas de salida de
inmediato. Códigos de salida del chequeo único: 0 bien, 1 faltó un precio o
una posición dio error, 2 la reconciliación detectó discrepancias. En
`--loop` el monitor sigue tras un fallo de precio o de base de datos y solo
se detiene (código 2) ante una discrepancia de reconciliación.

**Reanudar (`system_control resume`)** es siempre manual. Necesita el equity de
ese momento (precio de todas las posiciones abiertas; si falta uno no reanuda).
Escribe el evento `REANUDACION` con el pico anterior y fija el pico nuevo igual
al equity actual, y una fila de `equity_observaciones` con el mismo pico.

**Parada de emergencia:** crear un archivo vacío llamado `PARAR` en la raíz del
proyecto (la carpeta que contiene `app/`) frena las entradas nuevas (abrir y
aprobar) sin reiniciar nada; borrarlo suelta el freno. `system_control status`
avisa si está presente. Está en `.gitignore`.

**Reconciliación de v1:** para cada posición verifica que tenga exactamente una
orden de entrada ejecutada que coincida con su cantidad y precio, que
`cantidad restante = inicial - ventas ejecutadas`, que `take_profit_hit`
coincida con la existencia de la venta de TP parcial, que sus órdenes sean del
mismo símbolo y que, si está abierta, su símbolo siga en la lista autorizada; y
que ninguna orden ejecutada quede sin posición. No corrige nada. Cuando exista
Binance real se agregará la comparación contra el exchange.

**Tabla nueva `equity_observaciones` (migración `c5dbbf4b3553`):** solo-agregar
(trigger que rechaza `UPDATE`/`DELETE`), con `peak_equity_usd >= equity_usd` y,
si el origen es `REANUDACION`, `pico = equity`. Para aplicarla:
`docker compose run --rm app alembic upgrade head` y lo mismo sobre la base
`_test` (ver "Tests").

**Corregido en este bloque:** `ordenes.created_at` usaba el reloj de la base de
datos mientras que "entradas de hoy" se decide con el `now` del servicio; con
una fecha de prueba distinta de la real, el tope de 3 entradas por día fallaba
(dos tests empezaron a fallar al cambiar el día). Ahora los servicios guardan
`created_at = now` (el reloj lógico), que además es lo reproducible para el
backtest.

Pendiente para el Paso 11 (necesita Telegram): los avisos de escalamiento (caída
>5% en una hora, volumen >3σ, Binance caído >5 min) y empaquetar el monitor
como proceso programado (APScheduler). Tests:
`tests/unit/test_equity_rules.py`, `tests/unit/test_reconciliation.py`,
`tests/unit/test_emergency_stop.py`, `tests/integration/test_position_manager.py`,
`tests/integration/test_monitor.py`, `tests/integration/test_monitor_scripts.py`,
`tests/integration/test_equity_observaciones.py`,
`tests/integration/test_emergency_stop_entries.py`.

## Resultados, snapshots y métricas (Paso 9, 2026-10-05)

Todo sigue siendo dinero SIMULADO y sin IA: las fórmulas viven en
`app/domain/rules/results.py` (puras, `Decimal`, 100% cubiertas) y los
servicios de `app/results/` solo juntan datos, llaman a esas funciones y
guardan. El flujo del plan (sección 8.2):

```
decisión → posición → trade_snapshots → trade_result → performance_metrics
```

**Qué guarda cada tabla (todas con `NUMERIC`, `TIMESTAMPTZ` en UTC):**

| Tabla | Qué es | Reglas en la base de datos |
|---|---|---|
| `trade_snapshots` | Trayectoria de una posición abierta: un snapshot por chequeo (`SCHEDULED`) y uno por evento (`ENTRY`, `PARTIAL_TAKE_PROFIT`, `TRAILING_ACTIVATED`, `TRAILING_UPDATED`, `SYSTEM_PAUSED`, `EXIT`) | Solo-agregar (trigger). Único por `(posición, instante, estado)`: un reintento no duplica |
| `trade_results` | Resultado definitivo de una operación cerrada: P&L bruto/neto, comisiones, R-multiple, MAE/MFE, scores iniciales, estado de tesis, motivo de salida, benchmark | Solo-agregar (trigger). Un solo resultado por posición (`position_id` único). CHECK de coherencia |
| `performance_metrics` | Métricas agregadas: global, por activo y por tercil del score | Derivada y reconstruible: recalcular el mismo periodo REEMPLAZA la fila |

Además `posiciones.reference_prices_at_entry` (JSONB, se escribe una sola vez
al abrir): los precios de BTC y de los 20 pares observados en ese instante.

### Decisiones del Paso 9 (cerradas con el usuario el 2026-10-05)

1. **Salidas parciales → un solo resultado.** `exit_quantity` = suma de las
   ventas; `exit_price` = promedio ponderado por cantidad; `exit_fee` = suma de
   comisiones; `exit_order_id`, `exit_time` y `exit_reason` son los de la orden
   que CERRÓ la posición (p. ej. `TRAILING_STOP`). El P&L usa el flujo real de
   órdenes, no un único precio de salida.
2. **`thesis_status` por desenlace**, sin leer el texto libre de la invalidación
   ni usar un LLM. En orden: `STOP_LOSS` → `INVALIDATED`; salida ajena a la tesis
   (manual, drawdown del sistema, reconciliación, emergencia, otra) →
   `INCONCLUSIVE`; P&L neto > 0 con TP parcial ejecutado → `CONFIRMED`; P&L neto
   > 0 sin TP → `PARTIALLY_CONFIRMED`; cualquier otro caso → `INCONCLUSIVE`. Cuando
   la invalidación sea una condición medible (cambio del esquema del
   Consolidador) se podrá refinar; hoy no lo es.
3. **Benchmark con precios OBSERVADOS al abrir y al cerrar** (idea del usuario,
   en vez de las velas, que solo se ingieren una vez al día). El monitor de 60 s
   ya consulta precios; al abrir se guardan los de BTC y los 20 pares, y al
   cerrar se vuelven a consultar. `btc_return_during_trade_pct` es el retorno de
   BTC entre ambos instantes y `market_return_during_trade_pct` el promedio
   SIMPLE de los 20 pares. Si falta el precio de cualquier par en cualquiera de
   los dos instantes, el campo queda `NULL` (nunca se inventa ni se promedia con
   menos pares). Costo: ~20 consultas extra solo al abrir y al cerrar.
4. **`score_discrimination`** = expectancy del tercil superior − la del
   inferior, con expectancy = P&L neto medio en USD (todas las posiciones son de
   100 USD). Score = `puntaje_ponderado` inicial. Orden descendente, empates por
   fecha de entrada y luego por id; `k = n // 3` por extremo, el resto queda fuera.
   Bootstrap de 1.000 iteraciones con semilla fija (`20261005`): remuestreo con
   reemplazo, terciles recalculados en cada remuestreo, IC 95% por rango más
   cercano (elementos 25 y 975 de 1.000). Discrimina si el límite inferior es
   > 0. Con menos de 3 operaciones con score: `MUESTRA_INSUFICIENTE`; de 3 a 29:
   `PRELIMINAR` (se calcula pero el plan pide 30); desde 30: `EVALUABLE`.

### Derivado del plan y aprobado por el usuario (2026-10-05)

Estos supuestos los propuse yo (el plan no los define) y el usuario los aprobó
explícitamente el 2026-10-05; cambiarlos requiere una nueva decisión.

- **Registrar nunca bloquea operar.** Los snapshots y el `trade_result` se
  escriben en savepoints propios, después de la orden: si fallan, se registra el
  error y el stop/entrada sigue su curso. El monitor repara en cada chequeo las
  posiciones cerradas sin resultado (`ensure_trade_results`; sin precios de
  salida, así que sus benchmarks quedan `NULL`).
- **Sin `trade_snapshots.trade_result_id`.** El plan lo lista, pero rellenarlo al
  cerrar exigiría ACTUALIZAR un snapshot, y la regla del propio plan es no
  sobrescribirlos. El vínculo es `position_id`.
- **Snapshots que no se emiten todavía:** `TARGET_REACHED` (coincide siempre con
  `PARTIAL_TAKE_PROFIT`), `STOP_UPDATED` (el stop no se mueve en v1; el trailing se
  ve en `TRAILING_UPDATED`), `RISK_ALERT` (escalamientos, Paso 11) y
  `RECONCILIATION` (el monitor se detiene sin precios ante una discrepancia).
  Son valores válidos de `system_state`.
- **Columnas de un snapshot:** `quantity` es la cantidad ANTES de la venta en
  `PARTIAL_TAKE_PROFIT` y `EXIT`; `unrealized_pnl` = `cantidad × (precio −
  entrada)` sin comisiones; `stop_price` es el stop inicial o, con trailing, su
  nivel (máximo × 94%); `target_price` es `NULL` tras el TP parcial;
  `distance_to_stop_pct` = `(precio − stop) / precio × 100`; `market_volume` es
  `NULL` (la fuente de precios solo entrega precio); `atr` y `relative_volume`
  son de la temporalidad de 1h; todos los indicadores salen de velas ya CERRADAS
  en ese instante (sin look-ahead) y pueden tener horas de antigüedad, porque se
  recalculan una vez al día.
- **`capital_at_entry`** = equity de la última observación anterior a la entrada,
  o el capital paper si no había ninguna. No interviene en ningún cálculo de v1.
- **`initial_risk_score` e `initial_confidence_score` quedan `NULL`:** el sistema
  solo produce un score numérico (`puntaje_ponderado` → `initial_opportunity_score`);
  riesgo y confianza son cualitativos (están en el `decision_snapshot`).
- **`total_return_pct`** = P&L neto sobre el capital inicial del sistema;
  `max_drawdown_*` se guardan negativos (o 0) como en la fórmula del plan, y solo
  en el alcance `GLOBAL` (curva de `equity_observaciones`); el benchmark de
  periodo (BTC comprado y mantenido, velas de 1h ya cerradas) también solo en
  `GLOBAL`; `expectancy_pct` usa perdedoras/total como probabilidad de perder
  (los equilibrios aportan 0).
- **No existe el alcance "estrategia"** de `performance_metrics`: no hay
  versiones de estrategia registradas todavía.
- **`TODO — DECISIÓN PENDIENTE`:** mecanismo de corrección auditable de un
  `trade_result` (el plan pide "nueva versión", no reescribir). Ninguna regla de
  v1 lo necesita; hasta definirlo, una corrección exige una decisión humana.

### Comandos

```
docker compose run --rm app python -m scripts.results trades                 # operaciones cerradas
docker compose run --rm app python -m scripts.results performance [--days 30]  # recalcula y muestra métricas
docker compose run --rm app python -m scripts.results validate               # ¿el score discrimina?
```

`performance` guarda en `performance_metrics`; `validate` no guarda nada. Hasta
tener ~30 operaciones el resultado de `validate` es solo orientativo
(`PRELIMINAR`): el plan fija ese mínimo para dar una respuesta.

## Interfaz local (Paso 10a lectura, Paso 10c acciones)

Interfaz Streamlit para mirar los resultados. Todas las páginas **leen** en una
transacción de solo lectura. Las **únicas acciones** (Paso 10c) son tres, siempre
con confirmación en dos pasos: aprobar una entrada pendiente, rechazarla y
reanudar el sistema tras una pausa por drawdown. Siguen existiendo los comandos de
terminal (`scripts.entries`, `scripts.system_control`) y hacen lo mismo.

### Cómo abrirla

```
docker compose build              # una vez (y al cambiar dependencias): imagen compartida con `app`
docker compose up -d ui
```

Y abrir <http://localhost:8501>. Para detenerla: `docker compose stop ui`.

### Qué muestra (ocho páginas)

Dashboard, Análisis (último ciclo), Posiciones, Operaciones (filtros + detalle
con la decisión congelada y la trayectoria), Performance (por periodo, símbolo y
tercil de score), Validación de estrategia (las seis preguntas de la sección
8.7; las que no se pueden responder hoy dicen `PENDIENTE`), Decisiones
(historial de ciclos) y Sistema (base de datos, monitor, reconciliación,
frescura de velas, costo y saldo estimado de IA).

Un dato ausente se muestra como `n/d`, nunca como 0. Cada indicador/métrica del
catálogo (`app/ui/definitions/catalog.py`, versionado) lleva su tooltip con
definición, cómo leerlo, unidad y limitaciones; si una definición cambia, un
test exige subir su versión.

### Decisiones (cerradas con el usuario)

- 10a es solo lectura; 10c (acciones) y 10b (Entrevistador) ya están hechos.
- Las lecturas viven en un paquete nuevo, `app/services/`; la UI solo importa
  `app.services.views` (y los enums del dominio para los filtros).
- **La solo-lectura la garantiza la base**: `read_session()` abre la transacción
  con `SET TRANSACTION READ ONLY` y la descarta siempre; una escritura falla en
  PostgreSQL aunque alguien se equivoque en el código.
- La UI no calcula P&L, score ni riesgo y no ejecuta SQL. Los servicios
  muestran valores guardados o llaman a las funciones puras del dominio ya
  existentes; no inventan cifras nuevas.
- **Seguridad:** el contenedor escucha en `0.0.0.0` pero el puerto solo se
  publica en `127.0.0.1` del PC. La UI **no recibe** las claves de Binance ni el
  token de Telegram: el servicio `ui` no usa `env_file`, solo una lista explícita
  de variables (base de datos, clave de Anthropic para el Entrevistador, saldo
  declarado y capital/posiciones, que no son secretos), y el archivo `.env` real,
  que el volumen del repo dejaría visible dentro del contenedor, queda tapado por
  `docker/ui-sin-secretos.env`. Si la interfaz necesita una variable nueva, se
  agrega a mano en `docker-compose.yml` (un test lo vigila).
  Tras cambiar esto: `docker compose up -d ui` recrea el contenedor. Para
  comprobarlo:

  ```bash
  docker compose exec ui sh -c "env | grep -c -E 'BINANCE|TELEGRAM'"       # debe imprimir 0
  docker compose exec ui sh -c "grep -c -E 'KEY|TOKEN|SECRET' /app/.env"    # debe imprimir 0
  ```

  (El `| grep` va DENTRO de `sh -c` para que funcione también en PowerShell,
  que no tiene `grep`.)

### Acciones manuales (Paso 10c)

| Acción | Dónde | Qué hace |
|---|---|---|
| Aprobar entrada | Posiciones | Abre la posición paper al precio de Binance **de ese momento** (stop -8 % y objetivo +12 % sobre ese precio). |
| Rechazar entrada | Posiciones | Libera los cupos que reservaba; motivo opcional. Permitido aun en pausa. |
| Reanudar el sistema | Sistema (solo aparece si está pausado) | Reinicia el pico de equity al equity del momento; el pico anterior queda registrado. |

Cómo funciona y qué garantiza:

- **Dos pasos:** el botón solo muestra qué va a pasar; nada se ejecuta hasta
  pulsar *Confirmar* (o se cancela). El resultado se muestra una vez, arriba.
- **Mismas reglas que la terminal:** `app/services/commands.py` llama a las
  mismas funciones de `app/execution` (`approve_pending_entry`,
  `reject_pending_entry`, `resume_system`). No hay reglas nuevas: aprobar sigue
  bloqueado en pausa y con el archivo `PARAR`, las entradas vencidas se expiran y
  sin precio válido no se ejecuta nada (el botón de aprobar aparece
  deshabilitado en pausa o con PARAR).
- **Una transacción por acción:** si algo falla no queda nada a medias y la
  pantalla muestra un mensaje claro (el detalle va al registro, sin secretos).
  Repetir una aprobación no duplica la entrada (`client_order_id`).
- **Separación:** `commands.py` es el único módulo de `app/services` que escribe y
  el único que puede importar `order_manager`, `price_source` y `system_control`;
  ningún servicio de lectura lo importa (test de arquitectura).
- **Seguridad:** sin credenciales (el precio sale del ticker público); el
  contenedor sigue publicado solo en `127.0.0.1`. **No se puede crear ni borrar
  el archivo `PARAR` desde la interfaz** (se hace a mano, como antes).

### Perfil de inversión (Paso 10b)

Página **Perfil**: una entrevista con el Agente 1 (Entrevistador) de hasta 8
preguntas, una a la vez, con el prompt de la sección 7.2 del plan. Al terminar
muestra el perfil propuesto y **solo se guarda si lo confirmas**.

Decisiones (cerradas con el usuario el 2026-10-06):

- **Es informativo.** El perfil no entra al contexto de ningún agente, ni al
  score, ni a ninguna regla: así se conserva P1/P2, la reproducibilidad y la
  medición de si el score predice algo. Un test de arquitectura verifica que
  nada fuera de `services/interview.py` y `services/profile.py` toca su tabla.
  Si algún día debe influir, será una decisión nueva (versionar prompt y perfil
  en el `decision_snapshot`).
- **Tabla nueva `perfil_usuario`, solo-agregar** (un trigger rechaza UPDATE y
  DELETE; rangos con restricciones). Cada entrevista guardada es una fila; la
  vigente es la más reciente y las anteriores quedan como historia. Migración
  `57b7cfeafc9b`: **ejecuta `alembic upgrade head`** tras actualizar.
- **Qué se guarda:** solo el perfil final (el JSON de la sección 7.2) con el
  modelo y la versión del prompt. **Las respuestas del usuario no se guardan
  como texto.** Las preguntas del agente y el costo de cada llamada quedan en
  `llamadas_ia` (y cuentan en el costo y el saldo estimado de la página Sistema).
- **Privacidad:** tus respuestas viajan a Anthropic (es una llamada al modelo) y
  el perfil se queda en tu base local. Tope de 2000 caracteres por respuesta.
- **El límite de 8 preguntas lo impone el código:** tras la octava respuesta el
  código pide el JSON final con un mensaje de cierre (`[SISTEMA] ...`); si el
  modelo aún pregunta, se muestra un error y se puede reintentar o reiniciar.
- **Sin herramientas (P4):** el agente solo recibe y devuelve texto; no importa
  nada de ejecución ni de datos de mercado (test). La interfaz y los servicios no
  usan credenciales del exchange (test).
- La conversación vive en la sesión del navegador (no es estado operativo): si
  recargas la página, empiezas de nuevo. Necesita `ANTHROPIC_API_KEY` en `.env`.
- Un perfil fuera del formato (campo faltante, valor fuera de rango) se rechaza,
  nunca se corrige; la llamada fallida igual queda registrada con su costo.

### Limitaciones conocidas (mostradas tal cual en pantalla)

- El resultado del pre-filtro de ciclos pasados no se guarda; los "candidatos
  evaluados" se reconstruyen de las llamadas al Analista cuantitativo.
- El análisis por factor (`factor_analysis`, sección 8.8) está `PENDIENTE`.
- No hay caché: cada interacción vuelve a leer la base (suficiente para uso
  personal; se añade si se vuelve lenta).

## Orquestación del ciclo diario (Paso 7 del plan)

```bash
python -m scripts.run_daily_cycle  # ciclo real completo — consume API de pago si hay candidatos
```

`app/orchestration/candidate_pipeline.py` arma el bloque DATOS de cada
agente para un símbolo leyendo `velas`/`indicadores`/`eventos`/
`analisis_eventos` YA calculados en la base (nunca los recalcula — ver más
abajo por qué la ingesta y el cálculo de indicadores quedan fuera de este
paso). `app/orchestration/daily_cycle.py::run_daily_cycle` es el
orquestador real: corre el pre-filtro, evalúa a la vez a TODOS los
candidatos que devuelve (hasta `MAX_CANDIDATOS_PREFILTRO=5`, no uno a la
vez como el script de humo del Paso 6), llama a los 4 agentes por
candidato en su orden real de dependencia, calcula el score de cada opción
del Consolidador en código (`app/domain/rules/scoring.py`, nunca el LLM) y
persiste todo con trazabilidad completa: `ciclos_analisis` (una fila por
ejecución, con `status` formalizado en `app.domain.enums.EstadoCiclo`),
`propuestas` (una fila por CADA opción que devuelve el Consolidador,
supere o no el umbral de entrada) y `factores` (las 4 categorías
ponderadas de cada propuesta, con el peso fijo que realmente se usó).

**`EstadoCiclo`** (`app/domain/enums.py`) tiene deliberadamente solo 5
valores — `PREFILTER`, `EVENT_CORRELATION`, `AI_ANALYSIS`, `CONSOLIDATE`,
`FREEZE_DECISION_SNAPSHOT` — de los 15 que describe la sección 15.6 del
plan. Quedan fuera `INGEST`/`VALIDATE_DATA`/`CALCULATE_INDICATORS` porque
ese orquestador lee datos ya calculados por procesos separados y todavía
manuales (`scripts/backfill_candles.py`, `scripts/calculate_indicators.py`
— la actualización incremental programada sigue pendiente, ver "Qué NO
está implementado"); y quedan fuera `RISK_VALIDATION` en adelante porque
pertenecen al motor de ejecución (Paso 8) y a resultados/performance (Paso
9), que todavía no existen. El resultado de un ciclo (falló / terminó sin
candidatos / terminó con propuestas) se deriva de `status` +
`error_message` + la existencia de filas en `propuestas` — nunca de un
campo adicional inventado para resumirlo (ver el docstring de
`EstadoCiclo`).

**Reglas de corte** (ninguna nueva — ya decididas en pasos previos, o
confirmadas explícitamente con el usuario para este paso, 2026-09-30):
sin candidatos del pre-filtro, cero llamadas a la API; un candidato cuyo
Auditor marca `calidad_global="insuficiente"` queda excluido del bloque
que recibe el Consolidador (si todos quedan excluidos, el Consolidador ni
se llama); se persisten TODAS las opciones del Consolidador, trazabilidad
completa; `decision_snapshot` (JSONB dentro de `propuestas`, sección 8.3
del plan) congela indicadores, interpretación de los agentes, contexto de
eventos, auditoría de datos, scores, tesis/invalidación y versión de
modelo/prompt de cada agente — deliberadamente SIN `initial_stop`/
`initial_target` todavía, porque no existe `app/domain/rules/risk.py`
(`TODO — DECISIÓN PENDIENTE`, Paso 8).

**P5 (el sistema debe poder morir y revivir)**: `run_daily_cycle` hace
`session.commit()` después de cada bloque de costo real (creación del
ciclo, cada candidato evaluado, la llamada al Consolidador, cada propuesta
persistida) — mismo patrón que `candle_ingestion.backfill_all` — para que
un error posterior nunca se lleve por delante una llamada que ya costó
dinero de verdad. Un fallo no manejado deja el ciclo con `error_message`
no nulo y `status` = la última etapa alcanzada, nunca un salto silencioso.

Tests contra Postgres real con `call_agent` parcheado (nunca capas más
internas, igual que `test_consolidator_agent.py`):
`tests/integration/test_candidate_pipeline.py` (armado de DATOS) y
`tests/integration/test_daily_cycle.py` (ciclo sin candidatos, ciclo
completo con 2 candidatos, exclusión parcial/total por calidad
insuficiente, fallo de un agente).

**Fix real (2026-09-30):** con 5 candidatos en un ciclo real, la respuesta
del Consolidador se cortó por `max_tokens` insuficiente (`stop_reason` no
se revisaba antes de intentar parsear el JSON). Ver `app/ai/client.py`
(detección explícita de truncamiento) y
`app/ai/agents/consolidator.py::MAX_TOKENS_CONSOLIDADOR`.

**Fix real (2026-10-02):** el mismo tipo de corte pasó en el Analista de
eventos (Agente 3), no en el Consolidador — con 5-6 eventos del catálogo
en alcance para un símbolo, su respuesta se cortó a mitad de JSON con el
default de `run_json_agent` (4096 tokens), abortando el ciclo completo
después de ya haber pagado las llamadas previas de ese símbolo. Mismo
mecanismo de fix que el Consolidador: `app/ai/agents/events.py::
MAX_TOKENS_ANALISTA_EVENTOS` (8000, porque `eventos_comparables` crece con
la cantidad de eventos en alcance, sin tope fijo). Se subió también,
preventivamente, el del Analista cuantitativo (Agente 2,
`MAX_TOKENS_ANALISTA_CUANTITATIVO`) por compartir la misma forma de riesgo
(`observaciones` también es una lista de texto libre sin tope) — ese no
había fallado todavía en producción, a diferencia del de eventos.

**Alerta temprana de tokens (2026-10-02):** subir `max_tokens` después de
cada corte real (Consolidador, luego Eventos) funciona, pero siempre un
paso atrás — se descubre recién cuando el ciclo ya abortó y se perdió el
costo de las llamadas previas de ese símbolo. `app/ai/agents/_runner.py`
ahora registra un `WARNING` (`agente`, `modelo`, `output_tokens`,
`max_tokens`, `ciclo_id`) cada vez que una llamada que SÍ terminó bien usó
85% o más de su `max_tokens` — una señal de que el margen se está
agotando, antes de que efectivamente no alcance. No cambia ningún
resultado ni aborta nada; es puramente diagnóstico (sección 24 del plan).
Esto no reemplaza subir el límite cuando corresponda — es la forma de
enterarnos con datos reales (`output_tokens` por agente, vía esta alerta o
consultando `llamadas_ia` directamente) en vez de seguir ajustando
`max_tokens` a ojo, como se hizo con 8000 para Eventos/Cuantitativo.

**Observación real (2026-10-04) — "Sin propuestas" sin explicación:** al
terminar un ciclo sin propuestas, `scripts/run_daily_cycle.py` solo imprimía
"Sin propuestas este ciclo.", sin distinguir entre sus cuatro causas
posibles, y la única forma de saberlo era consultar la base. Ahora
`DailyCycleResult` expone también lo que respondió el Consolidador
(`consolidador_recomendacion`, `consolidador_razon_no_invertir`,
`consolidador_confianza_global`; todos `None` si el ciclo ni llegó a
llamarlo) — copiado tal cual de la respuesta ya validada, sin recalcular
nada (P1) — y el script imprime debajo del aviso una línea `Motivo:` según
el caso: (1) el pre-filtro no dejó candidatos, (2) todos quedaron excluidos
por calidad de datos y el Consolidador no se llamó, (3) el Consolidador
recomendó `no_invertir` (se imprime su razón textual y su confianza
global), o (4) un caso que no debería ocurrir (el schema rechaza `invertir`
sin opciones), que se declara como "motivo no determinado" en vez de
inventar una causa (P3). No cambia ninguna decisión del ciclo ni la base de
datos; es solo salida. Tests: `tests/unit/test_run_daily_cycle_script.py`
(los cuatro textos) y `tests/integration/test_daily_cycle.py` (el resultado
lleva la razón del Consolidador).

## Tarea diaria programada (Windows)

El ciclo diario no necesita que ninguna IA lo arranque ni lo supervise —
es un script determinista (principio P2 del plan). Se registra en el
**Programador de tareas de Windows** nativo, no como una tarea programada
de un asistente de IA, para que corra todos los días sin depender de que
una sesión de Claude esté abierta y vinculada a esta máquina a esa hora.

`scripts/run_daily_cycle.ps1` es el wrapper que Task Scheduler ejecuta: se
asegura de que Docker Desktop esté corriendo (lo levanta y espera hasta 2
minutos si hace falta), levanta los contenedores y corre
`scripts.run_daily_pipeline` dentro del contenedor `app`, con la salida
guardada en `logs/daily_cycle_<fecha>.log` (no se sube a git — ver
`.gitignore`) y espejada en vivo a la consola cuando corre en primer plano
(ver bug real más abajo).

**Para probar a mano, corré el script directo, no `schtasks /run`:**

```powershell
powershell -File scripts\run_daily_cycle.ps1
```

`schtasks /run` lanza la tarea registrada en un proceso aparte y vuelve de
inmediato — es "fire and forget" por diseño de Windows, nunca te va a
mostrar el progreso en la consola donde escribiste el comando, sin importar
qué tan bien loguee el script (ver bug real 2026-10-02, b). Sirve para
confirmar que la tarea programada en sí está bien registrada (credenciales,
disparador), no para ver una corrida en vivo. Corriendo el script directo
en cambio es síncrono: se queda en la misma ventana, imprime cada paso a
medida que pasa, y termina con `=== PROCESO FINALIZADO CON EXITO ===` o
`=== PROCESO FINALIZADO CON ERROR (codigo N) ===`.

**Bug real (2026-10-02):** el ciclo llevaba dos días corriendo sobre velas
con más de 50 horas de antigüedad — visible en los `WARNING` del Auditor
("Antigüedad crítica") en ambas corridas (2026-09-30 y 2026-10-01). Nada
mantenía `velas`/`indicadores` al día automáticamente: `run_daily_cycle.py`
asume por diseño (Paso 7) que esos datos ya están frescos, porque la
ingesta y el cálculo de indicadores son pasos separados. `scripts/
run_daily_pipeline.py` encadena, en orden, `backfill_candles --days 5`
(ventana chica, barata e idempotente — no los 2 años completos de la carga
inicial) → `calculate_indicators` → `run_daily_cycle`, deteniéndose si
algún paso falla. Esto no reabre la decisión de Paso 7: `daily_cycle.py`
sigue sin ingerir ni calcular nada por sí mismo.

**Bug real (2026-10-02, segundo hallazgo):** al probar `schtasks /run` tras
el fix anterior, la tarea se disparó y murió en segundos — el log mostraba
`ERROR no controlado:  Container crypto_trading_postgres Running` apuntando
a la línea del `docker compose run`. No era un fallo real: `docker compose`
escribe su progreso normal ("Container ... Running", "Creating", etc.) por
stderr, no por stdout, y con `$ErrorActionPreference = "Stop"` activo a
nivel de script, Windows PowerShell 5.1 convierte **cualquier línea de
stderr de un comando nativo** (no de un cmdlet de PowerShell) en un error
terminante — confundía progreso informativo con una excepción real y
cortaba el script antes de que Docker terminara de responder. Corregido con
un helper `Invoke-Native` que baja `$ErrorActionPreference` a `"Continue"`
solo mientras dura la llamada nativa (restaurando el valor anterior en un
`finally`) y valida el resultado real a mano con `$LASTEXITCODE` — la forma
correcta de detectar un fallo de un ejecutable externo en PowerShell. Se
aplicó a los tres llamados nativos del script (`docker info`,
`docker compose up -d` y `docker compose run ... run_daily_pipeline`).

**Bug real (2026-10-02, b):** el script solo escribía al archivo de log,
nunca a la consola — tenía sentido para la tarea programada desatendida
(nadie mira esa ventana), pero al probarlo a mano la consola no mostraba
nada: ni que seguía corriendo, ni si terminó bien o mal, obligando a abrir
una segunda ventana con `Get-Content -Wait` para ver algo. Corregido
espejando toda la salida a consola además del archivo: `Write-Log` ahora
también hace `Write-Host`, y los dos llamados a `docker compose` pasan por
`Tee-Object -FilePath $logFile -Append` en vez de `*>> $logFile` (que solo
mandaba al archivo). `Tee-Object` es un cmdlet de PowerShell, no un
ejecutable nativo, así que no pisa `$LASTEXITCODE` — sigue siendo el código
real de `docker`, el mismo que ya se valida a mano. Esto no sirve para
`schtasks /run` (ver la nota de arriba sobre por qué esa consola nunca
muestra nada, con o sin este fix) — es para cuando corrés el script
directo.

**Bug real (2026-10-02, c):** el texto de las tesis en el log salía con un
espacio de más entre cada letra ("C a n d i d a t o s") y las tildes/ñ
como "%�". Dos causas combinadas: Windows PowerShell 5.1 decodifica la
salida de un comando nativo (acá, `docker compose`, que reenvía la de
Python dentro del contenedor) usando `[Console]::OutputEncoding`, que por
defecto no era UTF-8 en esta máquina — así que el texto con tildes llegaba
corrupto desde el vamos; y `Tee-Object` (a diferencia de
`Out-File`/`Add-Content`) no tiene parámetro `-Encoding`, así que escribía
al log con el default de Windows PowerShell para ese cmdlet (UTF-16LE),
mezclado en el mismo archivo con las líneas de `Write-Log` (UTF-8 a mano).
Un ASCII en UTF-16LE es ese byte seguido de un byte `0x00`; leído con
cualquier herramienta que asuma UTF-8, ese `0x00` se ve como un espacio de
más — de ahí el patrón letra-espacio-letra. Corregido fijando
`[Console]::OutputEncoding`/`$OutputEncoding` a UTF-8 al inicio del script
(arregla la decodificación de origen, no solo el espaciado) y
reemplazando `Tee-Object` por `Write-TeeUtf8`, una función propia que
escribe a consola y archivo forzando UTF-8 en los dos lados — misma
codificación que `Write-Log` en todo el archivo. No cambia la detección
de fallo: `Write-TeeUtf8`, igual que `Tee-Object` antes, no es un
ejecutable nativo, así que no pisa `$LASTEXITCODE`.

**Bug real (2026-10-01):** la primera versión del script asumía que Docker
Desktop ya estaba corriendo. La tarea se disparó antes de que el usuario
lo hubiera abierto esa mañana, `docker compose` falló de inmediato, y ese
error puntual no quedó capturado por la redirección de streams — el log
quedó en 0 bytes, sin ninguna pista de qué pasó. Corregido verificando
`docker info` explícitamente antes de intentar nada, lanzando Docker
Desktop si hace falta, y envolviendo todo el script en try/catch con su
propio log — así cualquier error futuro, lo capture o no la redirección de
streams, queda registrado. Ruta de Docker Desktop: se resuelve vía
`%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe` (instalación por
usuario, confirmada en esta máquina — la ruta de máquina
`C:\Program Files\Docker\Docker\...` no aplica aquí); ajusta el script si
la tuya es distinta. Recomendado además: en Docker Desktop,
Settings → General → "Start Docker Desktop when you sign in", para no
depender nunca de que este script lo lance.

Registrar la tarea (PowerShell, una sola vez):

```powershell
schtasks /create /tn "CryptoTradingPlatform_DailyCycle" /sc daily /st 09:00 /tr "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"D:\Trabajo\Plataforma_de_Trading\crypto-trading-platform\scripts\run_daily_cycle.ps1`""
```

Ajusta `/st 09:00` a la hora que prefieras — el mercado cripto opera 24/7,
así que no hay una hora "correcta"; solo debe ser constante entre ciclos
para no sesgar la comparación de indicadores diarios. Verificar que quedó
registrada:

```powershell
schtasks /query /tn "CryptoTradingPlatform_DailyCycle"
```

Correr manualmente sin esperar al horario (útil para probar que quedó bien
configurada):

```powershell
schtasks /run /tn "CryptoTradingPlatform_DailyCycle"
```

**Si el PC está apagado a la hora programada (hallazgo 2026-10-05):** una
tarea creada con `schtasks /create` no trae activada la opción "ejecutar
lo antes posible si se perdió el inicio programado". Con el PC apagado a
las 10:00, Windows simplemente se salta esa corrida y no queda ningún log
(el script ni siquiera arrancó: `logs/` no tiene archivo de ese día). Para
que, al encender el PC, la tarea corra sola lo antes posible, activar esa
opción una sola vez (PowerShell):

```powershell
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Set-ScheduledTask -TaskName "CryptoTradingPlatform_DailyCycle" -Settings $s
```

Comprobar el último resultado de la tarea (`LastRunTime` y
`LastTaskResult`; 0 = terminó bien):

```powershell
Get-ScheduledTaskInfo -TaskName "CryptoTradingPlatform_DailyCycle"
```

Dos límites que conviene conocer: la tarea solo corre con tu sesión de
Windows iniciada (es el comportamiento de `schtasks /create` sin `/ru`),
así que con el PC encendido pero sin iniciar sesión tampoco corre; y un
ciclo que arranca después de las 19:00 hora de Colombia (00:00 UTC) cae en
el **día UTC siguiente**, lo que cambia a qué día cuentan sus entradas y
cuándo vencen las aprobaciones pendientes. Si el ciclo del día se perdió,
correrlo a mano con `powershell -File scripts\run_daily_cycle.ps1` antes
de esa hora.

**Costo estimado:** ~$0.49 USD por ciclo con candidatos reales (sección 4.3
del plan proyecta 12-20 llamadas diarias, por debajo de 8 USD/mes) — 30
días de ciclos completos rondan los 15 USD/mes.

**Qué valor tiene acumular esto antes de que exista Paso 8:** cada
`decision_snapshot` congela el contexto real en su momento (correcto para
trazabilidad), pero mientras no exista ejecución paper, ninguna propuesta
acumulada ahora se convierte después en una posición — su precio de
entrada queda obsoleto para cuando la ejecución esté lista. El valor de
correr esto ya es de calibración (con qué frecuencia el score supera 55,
qué tan seguido el Consolidador recomienda no invertir, estabilidad del
costo por ciclo), no un backlog de operaciones por ejecutar retroactivamente.

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

**Cada migración nueva se aplica a las DOS bases** (la real y la de pruebas);
si solo se aplica a la real, los tests fallan con `relation "..." does not
exist` (pasó con `perfil_usuario`, Paso 10b). Un solo comando lo hace por ti:

```bash
docker compose run --rm app python -m scripts.migrate_all
```

(o `python -m scripts.migrate_all` fuera de Docker). Corre `alembic upgrade head`
primero sobre la base real y luego sobre `<nombre>_test` (o `TEST_DATABASE_URL`
si la definiste); se detiene en el primer fallo y no imprime las URLs. Es
idempotente: si ya están al día, no cambia nada.

**Máquina limpia:** `docker compose up -d` ya crea ambas bases
automáticamente (ver `docker/init-test-db.sql`) — solo falta migrarlas con el
comando de arriba. Equivalente manual, por si lo necesitas:

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

y después migrar con `python -m scripts.migrate_all`.

Una vez que la base de pruebas exista y tenga las migraciones aplicadas,
`pytest` la usa automáticamente — no hace falta tocar `.env` (los tests
aislados unos de otros mediante una transacción externa que se revierte al
final de cada test, incluso si el código bajo prueba hizo `commit()`). Los
tests de integración de `exchange_client` además requieren salida a
internet hacia `api.binance.com` (sin API key).

**Bug real (2026-10-04) — test de configuración dependiente del entorno:**
`docker compose run --rm app pytest` fallaba en
`test_settings_defaults_match_plan_values` con `'' is None`, mientras que en
venv nativo pasaba. Causa: `docker-compose.yml` inyecta el `.env` completo
como variables de entorno del contenedor, y una variable definida pero vacía
(`BINANCE_API_KEY=`) gana sobre el default `None` de `Settings`, aunque el
test pase `_env_file=None` (eso solo apaga el archivo, no el entorno del
proceso). No era un bug de la aplicación sino de aislamiento del test:
ahora `tests/unit/test_config.py` borra del entorno todas las variables que
lee `Settings` (fixture autouse `_entorno_limpio`), así pasa igual en venv,
en Docker o en CI.

## Estructura del proyecto

```text
app/
├── main.py              # punto de entrada (comandos healthcheck, fetch-candles, calculate-indicators)
├── config.py             # configuración tipada (pydantic-settings)
├── logging_config.py     # logging estructurado en JSON
├── db/
│   ├── base.py            # Base declarativa de SQLAlchemy
│   ├── session.py         # engine y fábrica de sesiones
│   └── models/             # velas, indicadores, eventos, analisis_eventos,
│                           # ciclos_analisis/propuestas/factores, llamadas_ia
├── domain/
│   ├── symbols.py         # whitelist explícita de símbolos operables (sección 21)
│   ├── enums.py           # EstadoCiclo (etapas de ciclos_analisis.status, Paso 7), TierEntrada
│   └── rules/
│       ├── scoring.py     # umbrales fijos + calculate_consolidated_score (Paso 6)
│       └── entry_tier.py  # segunda ronda de entradas de mayor riesgo (determinista, puro)
├── data/
│   ├── exchange_client.py     # cliente mínimo de Binance mainnet (velas, solo lectura)
│   ├── candle_ingestion.py    # paginación + backfill idempotente hacia `velas`
│   ├── indicator_calculator.py  # RSI/ATR/SMA/volumen relativo/distancia a máximos (deterministas, puro)
│   ├── indicator_pipeline.py    # orquesta el cálculo sobre muchas combinaciones + persistencia
│   ├── indicator_verification.py # recalcula vs. persistido + invariantes matemáticos
│   ├── event_analyzer.py        # % de cambio de precio en ventanas alrededor de un evento (determinista, puro)
│   ├── event_pipeline.py        # orquesta el cálculo sobre todo el catálogo + persistencia
│   ├── prefilter.py             # puntaje 0-100 por símbolo (determinista, puro)
│   ├── relative_strength.py     # RSI del ratio SÍMBOLO/BTCUSDT con velas ya guardadas (determinista, puro)
│   └── prefilter_pipeline.py    # orquesta el pre-filtro sobre todo el universo
├── ai/
│   ├── client.py           # cliente delgado sobre el SDK de Anthropic (prompt caching)
│   ├── models.py           # nombres de modelo + precios por millón de tokens
│   ├── schemas.py          # schemas Pydantic de la salida de cada agente
│   ├── prompts/            # un prompt de sistema por agente
│   └── agents/
│       ├── interviewer.py      # Agente 1 — Entrevistador (conversacional, informativo; Paso 10b)
│       ├── _runner.py          # orquestación JSON-in/JSON-out compartida (2, 3, 4)
│       ├── quantitative.py     # Agente 2 — Analista cuantitativo
│       ├── events.py           # Agente 3 — Analista de eventos
│       ├── auditor.py          # Agente 4 — Auditor de datos
│       └── consolidator.py     # Agente 5 — Consolidador (score en código, no en el LLM)
├── orchestration/
│   ├── candidate_pipeline.py  # arma el bloque DATOS de cada agente desde la base (Paso 7)
│   └── daily_cycle.py         # run_daily_cycle: el ciclo real completo (Paso 7)
├── execution/              # motor de ejecución paper (Paso 8), sin IA
│   ├── price_source.py     # precio del ticker público de Binance (sin credenciales)
│   ├── order_manager.py    # entradas: abrir, aprobar, rechazar, expirar
│   ├── position_manager.py # cierres: stop, TP parcial, trailing (órdenes de salida)
│   ├── monitor.py          # un chequeo de 60 s: reconciliar, vigilar, equity, pausa
│   ├── reconciliation.py   # el estado de posiciones y órdenes cuadra
│   ├── system_control.py   # estado del sistema y reanudación manual
│   └── emergency_stop.py   # archivo PARAR
├── results/                # resultados de operaciones (Paso 9), sin IA
│   ├── snapshot_service.py     # trade_snapshots: trayectoria solo-agregar
│   ├── trade_result_service.py # trade_results: resultado único e inmutable por posición
│   ├── benchmark_service.py    # benchmark del periodo (BTC, velas 1h cerradas)
│   ├── performance_service.py  # performance_metrics (derivadas, reconstruibles)
│   └── validation_service.py   # ¿el score discrimina? (bootstrap)
├── services/               # lecturas para la interfaz (Paso 10a) y acciones (Paso 10c)
│   ├── session.py          # read_session(): transacción READ ONLY
│   ├── commands.py         # aprobar/rechazar entrada y reanudar (escritura)
│   ├── interview.py        # entrevista del Agente 1 y guardado del perfil (escritura + IA)
│   ├── profile.py          # perfil vigente (lectura)
│   ├── views.py            # fachada que importa la UI (una función por vista)
│   └── dashboard.py, positions.py, trades.py, performance.py,
│       validation.py, decisions.py, system.py
├── ui/
│   ├── app.py              # navegación de las 8 páginas (streamlit run app/ui/app.py)
│   ├── pages/              # una página por módulo (`render()`)
│   ├── components/         # tarjetas, tablas, gráficos, tooltips
│   └── definitions/        # catálogo versionado de definiciones (tooltips)
├── repositories/
│   ├── health.py           # health check de base de datos
│   ├── candles.py          # persistencia + lectura idempotente de velas
│   ├── indicators.py       # persistencia idempotente de indicadores
│   ├── events.py           # persistencia con upsert del catálogo de eventos
│   ├── event_analysis.py   # persistencia idempotente de analisis_eventos
│   ├── ai_calls.py         # persistencia de llamadas_ia
│   ├── perfil_usuario.py   # perfil del Entrevistador (solo-agregar; informativo)
│   └── analysis.py         # persistencia de ciclos_analisis/propuestas/factores
migrations/                # Alembic
scripts/
├── results.py                    # CLI: trades / performance / validate (Paso 9)
├── backfill_candles.py          # CLI: backfill histórico completo
├── calculate_indicators.py      # CLI: cálculo masivo de indicadores
├── verify_indicators.py         # CLI: verificación de integridad de indicadores
├── seed_events.py                # CLI: carga/actualiza el catálogo de eventos
├── calculate_event_analysis.py  # CLI: cálculo masivo de analisis_eventos
├── run_prefilter.py              # CLI: puntúa los 20 símbolos y muestra los candidatos del día
├── run_agent_demo.py             # CLI: un agente, datos de ejemplo — consume API de pago
├── run_agent_demo_full_cycle.py  # CLI: 4 agentes, un símbolo real, sin persistir — consume API de pago
├── run_daily_cycle.py            # CLI: ciclo real completo, persiste todo — consume API de pago; explica el "sin propuestas"
└── run_daily_pipeline.py         # CLI: encadena backfill (--days 5) + indicadores + run_daily_cycle
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
- Docker Compose con PostgreSQL 16 + TimescaleDB, y un servicio `app`
  (Dockerfile propio) para correr cualquier comando del proyecto dentro de
  Linux sin instalar nada en el sistema del desarrollador — ver "Ejecutar
  comandos dentro de Docker". Se agregó tras encontrar en la práctica que
  Smart App Control de Windows 11 bloquea las extensiones compiladas de
  psycopg/pydantic-core/SQLAlchemy sin importar la versión de Python.
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
- Orquestador del ciclo diario (`app/orchestration/`, sección 15.6/8.3 del
  plan, Paso 7): `daily_cycle.py::run_daily_cycle` corre el ciclo completo
  de análisis — pre-filtro, hasta 5 candidatos evaluados con los 4 agentes
  cada uno, Consolidador, scoring determinista — y persiste `ciclos_analisis`
  (con `status` formalizado en `app.domain.enums.EstadoCiclo`), `propuestas`
  (con `decision_snapshot` congelado) y `factores`. Comando
  `python -m scripts.run_daily_cycle`. Ver "Orquestación del ciclo diario
  (Paso 7 del plan)" más arriba.
- Resultados, snapshots y métricas (Paso 9): `trade_snapshots` y
  `trade_results` solo-agregar (triggers), `performance_metrics` derivadas y
  reconstruibles, fórmulas puras en `app/domain/rules/results.py`
  (`calculate_trade_result`, `calculate_performance`,
  `validate_score_discrimination`) y comandos `python -m scripts.results`.
  Ver "Resultados, snapshots y métricas (Paso 9)" más arriba.
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

- Agente 1 (Entrevistador) con su página de entrevista y la tabla `perfil_usuario` (Paso 10b).
- Interfaz Streamlit (Paso 10a y 10c): `app/ui/` (8 páginas, componentes,
  catálogo de definiciones versionado) sobre `app/services/` (lecturas en
  transacción `READ ONLY`; acciones manuales con confirmación). Ver "Interfaz local".

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
- Efecto del perfil sobre el análisis o las decisiones: ninguno por diseño (es
  informativo). Si se quisiera, requiere una decisión nueva.
- `initial_stop`/`initial_target` NO están en el `decision_snapshot` (que no
  se reescribe): se calculan al ABRIR sobre el precio de ejecución real y quedan
  inmutables en `posiciones.initial_stop_price` / `initial_target_price`
  (decisión del Paso 8).
- Validación de riesgo, aprobación (manual u órdenes paper) y ejecución de
  propuestas (sus reglas ya están decididas: ver "Decisiones del Paso 8") — `propuestas` queda como el registro de TODO lo que el
  Consolidador propuso, sin decidir todavía cuál se convierte en una
  posición real (Paso 8: `RISK_VALIDATION` en adelante de la sección 15.6).
- Monitor como proceso permanente y programado (APScheduler, Paso 11) y los
  avisos de escalamiento por Telegram (caída >5% en una hora, volumen >3σ,
  Binance caído >5 min): hoy el monitor se lanza a mano con
  `scripts.monitor --loop` y solo vigila mientras corre. Ninguna credencial de
  Binance se usa todavía; ni el cliente de velas, ni el backfill, ni el monitor
  la necesitan.
- Corrección auditable de un `trade_result` y alcance "estrategia" de las
  métricas (ver "Resultados, snapshots y métricas"), y los snapshots
  `RISK_ALERT` (Paso 11).
- Ajuste de pesos o umbrales con los resultados: el plan lo prohíbe sin
  registrar antes una nueva versión de estrategia (sección 8.7).
- Desde la interfaz no se puede abrir un ciclo, cerrar una posición a mano, crear/borrar
  el archivo `PARAR` ni cambiar ninguna regla de riesgo (fuera de alcance de 10c).
- Análisis por factor (`factor_analysis`) en la interfaz: pendiente.
- Conexión a Telegram.

## Cómo verificar que el entorno funciona (resumen)

Con venv nativo:

```bash
docker compose up -d
alembic upgrade head
python -m app.main healthcheck
pytest
```

O, sin instalar Python en el sistema (ver "Ejecutar comandos dentro de
Docker" — necesario en Windows con Smart App Control activado):

```bash
docker compose up -d
docker compose run --rm app alembic upgrade head
docker compose run --rm app python -m app.main healthcheck
docker compose run --rm app pytest
```

Una máquina limpia debe poder ejecutar estos cuatro comandos, en orden,
siguiendo únicamente este README, sin conocimiento previo del proyecto
(criterio de aceptación de la sección 16 del plan).
