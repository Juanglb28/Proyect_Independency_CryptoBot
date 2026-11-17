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
el mismo comando dos veces no duplica ninguna vela (restricción única
`symbol+timeframe+open_time`, sección 15.7 del plan), así que un corte a mitad
de camino se resuelve simplemente volviendo a correrlo.

### Velas cerradas vs. en curso (decisión del usuario, 2026-10-08)

Binance devuelve también la vela del período que **todavía se está formando**
(una por temporalidad). Antes se guardaba con `DO NOTHING` y quedaba congelada
para siempre con los valores parciales del momento de la descarga (y los
indicadores recursivos —RSI/ATR de Wilder, medias— heredaban el error). Ahora:

- La tabla `velas` tiene la columna `cerrada`. La vela en formación se guarda
  con `cerrada = false` y **se refresca en cada ingesta** hasta que cierra.
- Una vela es definitiva si Binance ya entregó una posterior de la misma serie
  o si pasaron 10 s de su hora de cierre (`app/domain/rules/candles.py`; el
  margen cubre un reloj local ligeramente adelantado).
- Una vela cerrada es **inmutable**: lo impone el upsert (`ON CONFLICT DO UPDATE
  ... WHERE cerrada = false`) y un trigger de la base (migración `e6a1c4b7d902`).
- Indicadores, análisis, benchmark y volumen atípico leen **solo velas cerradas**.
  El análisis diario ve, por tanto, “el último cierre” de cada temporalidad
  (el campo `ultima_vela_hace_horas` del Auditor se mide desde ese cierre).
- Si una vela guardada cambia, se descartan los indicadores derivados de ella y
  de las posteriores para recalcularlos desde velas cerradas.
- `scripts.backfill_candles` termina con código 3 si quedan velas **vencidas sin
  cerrar** (datos parciales antiguos); `run_daily_pipeline` se detiene ahí.

### Reparar el historial congelado (una sola vez)

La migración marca como `cerrada = false` las velas que se guardaron antes de
cerrar (criterio exacto: `created_at` anterior a su hora de cierre). Para
corregirlas:

```bash
python -m scripts.repair_partial_candles              # SIMULACIÓN: no guarda nada
python -m scripts.repair_partial_candles --ejecutar   # aplica
```

La simulación ejecuta el mismo camino de código y al final deshace todo; muestra
por serie cuántas velas cambian, el mayor cambio de cierre y el RSI-14 de antes
y después. Las propuestas y decisiones ya tomadas **no se tocan**: cada una guarda
su copia congelada de los datos con que se decidió (decision snapshot).

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
`scripts.run_prefilter` solo muestra el resultado y no guarda nada. El ciclo
diario (`app/orchestration/daily_cycle.py`) sí guarda, por cada ciclo, una fila
por símbolo del universo en `prefiltro_resultados` (ver abajo).

### Decisión congelada en la base de datos (`propuestas`, `factores`; migración `c9d3f7a1e5b2`, 2026-10-17)

La regla de la sección 14 del master prompt («una vez creada la propuesta, su
`decision_snapshot` no se modifica») se cumplía solo por convención: ningún código
actualiza esas filas. Ahora la impone la base de datos con un trigger
`BEFORE UPDATE` en cada tabla:

- `propuestas` rechaza el `UPDATE` si cambia el snapshot o cualquier dato que
  describe la decisión (puntajes, tesis, invalidación, símbolo, ciclo, tier,
  fuentes, segunda ronda...). Cambiar solo `created_at` u otra columna ajena a la
  decisión sigue permitido, y reescribir el mismo valor no cuenta como cambio.
- `factores` rechaza cambiar el factor, su peso, su puntaje o su confianza.
- **No se bloquea `DELETE` ni `TRUNCATE`** (a propósito, hasta revisar la limpieza
  de las pruebas y la copia a Railway). Esta migración cierra la modificación
  retroactiva, no el borrado.
- La copia a Railway ve el trigger y trata estas tablas como inmutables
  (`ON CONFLICT DO NOTHING`). **Aplicar la migración también en Railway** antes de
  la siguiente copia (`scripts\sync_railway.ps1 -MigrarRailway`), porque la copia
  exige la misma versión de esquema en origen y destino.

Código: `migrations/versions/c9d3f7a1e5b2_propuestas_y_factores_inmutables.py`.
Pruebas: `tests/integration/test_decision_congelada.py`. Un test que necesita
alterar un puntaje de factor para preparar datos desactiva el trigger solo dentro
de su transacción (mismo patrón que los tests de `trade_results`).

### Resultado del pre-filtro por ciclo (`prefiltro_resultados`)

Tabla nueva (aprobada por el usuario, 2026-10-07; migración `a3c2e5195411`),
**solo-agregar** (un trigger rechaza UPDATE y DELETE) y única por
`(ciclo, símbolo)`. Una fila por cada símbolo del universo y por cada ciclo, con
su estado (`CANDIDATO`, `DESCARTADO_POR_PUNTAJE`, `DESCARTADO_POR_TOPE`,
`DESCARTADO_POR_POSICION_ABIERTA` o `SIN_DATOS`), su ranking, la vela diaria usada, los valores crudos
(tendencia, volumen relativo, distancia a 90 días), los sub-puntajes, el puntaje
final y la **versión de la fórmula** (`PREFILTER_FORMULA_VERSION`; un test con
una huella obliga a subirla si cambian los límites, el umbral o el tope). Un
símbolo sin datos se guarda con los números vacíos, nunca con un valor de
relleno. Se guarda aunque el ciclo termine sin candidatos.

**Símbolos con posición abierta o entrada pendiente (2026-10-26).** El sistema no
aumenta posiciones: la validación de riesgo rechaza con `SIMBOLO_YA_ABIERTO` cualquier
entrada sobre un símbolo ocupado. Por eso el ciclo diario ya no lo analiza con IA: lo
quita de los candidatos **antes** del corte a los 5 mejores (el cupo pasa al siguiente)
y lo guarda como `DESCARTADO_POR_POSICION_ABIERTA`, con su puntaje y su ranking
intactos. "Ocupado" = posición `OPEN` de cualquier serie o entrada `PENDING_APPROVAL`
(`app/repositories/simbolos_ocupados.py`; un test comprueba que coincide con los cupos
de `build_cupos`). El ciclo horario tampoco lo cuenta como "candidato nuevo". No cambia
la fórmula ni la versión del pre-filtro (`PREFILTER_FORMULA_VERSION`). Los ciclos
anteriores no se tocan. Para aplicarlo hay que **recrear el scheduler**:
`docker compose up -d --force-recreate scheduler`.

Los ciclos anteriores a esta tabla **no se rellenan**: no hay forma honesta de
saber qué sabía el sistema ese día, y recalcular con los datos de hoy mezclaría
el presente con una decisión pasada. La interfaz (Análisis y Decisiones) muestra
el embudo "universo → con puntaje → candidatos" y el detalle por activo en los
ciclos nuevos, y conserva el aviso de limitación en los viejos.

> Tras actualizar: `docker compose run --rm app python -m scripts.migrate_all`.

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

**Prompt del Analista cuantitativo — v3 (2026-10-15).** Añade solo una sección
"CONVENCIÓN DE TIEMPOS": `vela_mas_reciente_utc` es la hora de *apertura* de la última vela
cerrada y cada temporalidad cierra en un momento distinto, así que la diaria con fecha del día
anterior y los cierres distintos entre temporalidades **no** son desfase ni incoherencia (la IA
los reportaba como baja fiabilidad). Los datos de entrada no cambian; la versión queda en cada
decisión. Pendiente, solo si las observaciones lo piden: añadir la hora de cierre de cada vela al
bloque DATOS (`claude/decisiones-prompt-cuantitativo-v3.md`).

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

### Tope diario de gasto de IA (decisión del usuario, 2026-10-10)

El tope diario es **2,00 USD por día UTC** (`AI_DAILY_BUDGET_USD`, por defecto
`2.00`, debe ser > 0; se puede cambiar en `.env`). Es un tope de COSTO, no una
regla de riesgo de trading: no toca capital, stops ni posiciones.

- **Tope blando, antes de empezar.** Antes de iniciar cualquier ciclo
  AUTOMÁTICO (el diario de las 00:05 UTC o el disparado cada hora por candidatos
  nuevos) el scheduler suma `llamadas_ia.cost_usd` del día UTC en curso. Si ya
  es ≥ al tope, el ciclo NO corre. Un ciclo que ya empezó termina aunque
  cruce el tope: no se corta a medias (dejaría análisis incompletos), así que
  el gasto de un día puede superar el tope por, como máximo, un ciclo.
- **Falla cerrada.** Si no se puede leer el gasto (error de base de datos), el
  ciclo tampoco corre: ante la duda no se gasta.
- **Un aviso por día** (`PRESUPUESTO_IA_AGOTADO`, Telegram); reiniciar el
  scheduler no lo repite.
- **El ciclo manual no se bloquea.** `python -m scripts.run_daily_cycle` sigue
  funcionando (tú decides gastar) y ahora imprime primero el gasto de hoy frente
  al tope.
- **Confirmación si ya hay un ciclo reciente** (2026-10-21, tras lanzar por error dos
  ciclos con 18 min de diferencia): si hay un ciclo terminado sin error, o aún en
  curso, dentro de las últimas 6 horas, el ciclo manual lo avisa y pide `s` para
  continuar (cualquier otra respuesta cancela, sin gastar IA y con código de salida 2).
  Un ciclo fallido no cuenta: reintentar tras un fallo no pide confirmación. Sin terminal
  interactiva (`docker compose run -T`, tarea programada) cancela en vez de quedarse
  esperando. `--forzar` salta la pregunta:
  `docker compose run --rm app python -m scripts.run_daily_cycle --forzar`. No afecta al
  scheduler. La regla vive en `app/domain/rules/ciclo_manual.py` (ventana en
  `VENTANA_CICLO_RECIENTE`); la pregunta, en `scripts/run_daily_cycle.py`.
- La página **Sistema** de la interfaz muestra el gasto de hoy frente al tope y
  avisa si está agotado.
- Sin migración: `alertas.tipo` es VARCHAR sin restricción.

### Guardia de saldo de IA (decisión del usuario, 2026-10-11)

Con poco crédito, un ciclo (≈0,50-0,55 USD con 5 candidatos) se quedaría sin saldo a
mitad de camino y se cobraría lo ya gastado. Como la API de Anthropic no expone el
saldo real, la guardia usa el que TÚ declaras en `.env`:

```bash
ANTHROPIC_SALDO_DECLARADO_USD=0.34
ANTHROPIC_SALDO_DECLARADO_EN=2026-10-11T17:30:00+00:00   # la hora (UTC) a la que lo leíste en la consola
# AI_MIN_BALANCE_PER_CYCLE_USD=0.60                      # opcional; este es el valor por defecto
```

- **Saldo estimado = saldo declarado − gasto registrado en `llamadas_ia` desde esa
  hora.** Es una ESTIMACIÓN: no incluye gasto que el sistema no pudo registrar (por
  ejemplo una corrida cortada a mitad). Recalíbralo cada vez que revises la consola.
- **Ciclos automáticos** (el de las 00:05 UTC y el disparado por candidatos nuevos): si
  el saldo estimado es menor que el mínimo (0,60 USD por defecto; igual alcanza), el
  ciclo NO arranca y llega UN aviso por día UTC (`SALDO_IA_INSUFICIENTE`). El monitor y
  los stops siguen (no usan IA). Se revisa DESPUÉS del tope diario y antes de tocar nada.
- **Falla cerrada**: si no se puede leer el gasto, el ciclo no arranca.
- **Opcional**: sin saldo y fecha declarados la guardia no actúa (no se inventa un
  número).
- **Ciclo manual** (`scripts/run_daily_cycle.py`): imprime una ADVERTENCIA antes de gastar
  si el saldo estimado es bajo, pero no se bloquea (lo lanzas tú o la tarea de Windows).
- El resumen de cada ciclo por Telegram añade el saldo estimado (y avisa si ya no
  alcanza para otro ciclo).
- **Al recargar**: pon el saldo real y la hora nueva en `.env` y reinicia el scheduler
  (`docker compose up -d --force-recreate scheduler`; las variables se leen al arrancar).
  Hora UTC actual en PowerShell: `(Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss+00:00")`.

### Verificación previa de la llave de IA (decisión del usuario, 2026-10-30)

El 30/10 la API key venció (se había creado con fecha de expiración). El ciclo diario
gastó ~17 minutos de backfill e indicadores antes de descubrirlo en la primera llamada de
IA (`401 invalid x-api-key`), y el ciclo horario lo reintentó cada hora. Ahora, antes de
empezar un ciclo **automático**, se hace una llamada mínima (1 token de salida, modelo del
auditor; una fracción de centavo) para comprobar la llave:

- **Aceptada**: el ciclo sigue como siempre.
- **Rechazada (401/403, o sin llave en el `.env`)**: el ciclo NO arranca (no se crea ni
  siquiera el ciclo en la base ni se hace el backfill) y llega UN aviso por día UTC por
  Telegram (`LLAVE_IA_RECHAZADA`) que explica qué hacer. Los reintentos del ciclo
  horario no repiten el aviso. El aviso nunca incluye la llave.
- **Cualquier otro fallo** (red caída, límite de uso, caída de Anthropic): el ciclo sigue;
  no demuestra que la llave esté mal y bloquear por una caída pasajera perdería ciclos válidos.
- Se revisa DESPUÉS del tope diario y de la guardia de saldo, y antes de tocar nada
  (`app/orchestration/ai_budget.py::verificar_llave_ia`; la llamada, en
  `app/ai/client.py::verificar_llave`).
- **No cubre el ciclo manual** (`scripts/run_daily_cycle.py`): ahí el error 401 sigue saliendo
  en la primera llamada de IA.
- **Prevención**: al crear la llave en la consola de Anthropic, elige sin expiración o
  apunta la fecha. La API no permite consultarla desde el sistema.
- Al arreglarla: pon la llave nueva en `ANTHROPIC_API_KEY` (`.env`) y recrea el scheduler
  (`docker compose up -d --force-recreate scheduler`; las variables se leen al arrancar).

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

Desde el Paso 11c, **abrir y aprobar entradas también reconcilian primero**
(`open_entries`, `approve_pending_entry`): con una discrepancia no se abre
ninguna entrada, la orden pendiente se queda pendiente (resultado
`BLOQUEADA_POR_RECONCILIACION`) y rechazar sigue permitido. Es la regla del plan
"bloquear operación hasta resolver", ahora respetada en el monitor **y** en las
entradas.

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

Los avisos de escalamiento (caída >5% en una hora, volumen >3σ, Binance caído
>5 min) existen desde el Paso 11a-2 (ver "Avisos por Telegram"); queda pendiente
empaquetar el monitor como proceso programado (hecho en el Paso 11b: ver `app/orchestration/scheduler.py`). Tests:
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
| `trade_results` | Resultado definitivo de una operación cerrada: P&L bruto/neto, comisiones, R-multiple, MAE/MFE, scores iniciales, estado de tesis, motivo de salida, benchmark | Solo-agregar (trigger). Un solo ORIGINAL por posición; las correcciones son versiones nuevas encadenadas (ver "Corrección auditable"). CHECK de coherencia |
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
- **Corrección auditable de un `trade_result`** (2026-10-13): implementada. Ver
  la sección siguiente.

### Corrección auditable de un trade_result (2026-10-13)

El plan §15.3 pide que un error de cálculo se corrija "registrando una nueva
versión/corrección auditable", nunca reescribiendo; §37.8 prohíbe modificar
resultados históricos para mejorar los números. `trade_results` sigue siendo de
solo-agregar (un trigger rechaza UPDATE/DELETE).

Una corrección es una **fila nueva** que apunta con `supersedes_id` a la que
corrige, con un `correction_reason` obligatorio y, opcional, `corrected_by`. La
original queda intacta. La versión **vigente** de una posición es la punta de la
cadena (la que ninguna otra corrige); toda la lectura de métricas, validación y
listados usa solo las vigentes, y la auditoría puede ver todas.

Garantías en la base: un índice único parcial permite un solo original por
posición y una sola corrección por versión (cadena lineal, sin bifurcaciones, y
solo se puede corregir la punta); un trigger exige que la corrección sea de la
misma posición. Una corrección **siempre recalcula** con `calculate_trade_result`
(nunca números a mano, §37.8); si el recálculo coincide con la versión vigente no
registra nada.

Comandos (la corrección es humana en el bucle: sin `--confirmar` solo simula):

```
# Simula qué cambiaría (no escribe nada):
docker compose run --rm app python -m scripts.results corregir --position <UUID> --motivo "error en ..."
# Aplica la corrección:
docker compose run --rm app python -m scripts.results corregir --position <UUID> --motivo "error en ..." --autor juan --confirmar
# Historial de versiones de una operación (auditoría):
docker compose run --rm app python -m scripts.results versiones --position <UUID>
```

Migración `f3a2d9c1b4e7`. Decisiones en `claude/decisiones-correccion-trade-result.md`.
Tests: `tests/unit/test_trade_result_correction_guards.py`,
`tests/integration/test_trade_result_correction.py`, y casos del CLI en
`tests/integration/test_results_script.py`.

### Comandos

```
docker compose run --rm app python -m scripts.results trades                 # operaciones cerradas
docker compose run --rm app python -m scripts.results performance [--days 30]  # recalcula y muestra métricas
docker compose run --rm app python -m scripts.results validate               # ¿el score discrimina?
```

**Medición adicional estratificada por mes (enmienda E4, 2026-10-12).** `validate` imprime, debajo de la
medición principal de §8.7, una segunda medición: el mismo método de terciles y bootstrap, pero sobre el P&L
neto de cada operación menos el promedio de su mes de entrada (UTC). Sirve para separar la capacidad del
score del régimen de mercado: el backtest walk-forward mostró que agrupar trimestres puede dar "discrimina"
(+0,37 pp en la muestra completa) aunque dentro de casi todos los trimestres no lo haga (4 de 14 positivos).
Los meses con una sola operación con score se excluyen y se cuentan; si la principal discrimina y la
estratificada no, el informe avisa de un posible efecto de régimen. No sustituye a la regla de §8.7 ni es una
puerta automática. También aparece en la página Validación. Definición y limitaciones:
`claude/enmiendas-al-plan.md` (E4).

`performance` guarda en `performance_metrics`; `validate` no guarda nada. Hasta
tener ~30 operaciones el resultado de `validate` es solo orientativo
(`PRELIMINAR`): el plan fija ese mínimo para dar una respuesta.

## Interfaz local (Paso 10a lectura, Paso 10c acciones)

Interfaz Streamlit para mirar los resultados. Todas las páginas **leen** en una
transacción de solo lectura. Las **únicas acciones** (Paso 10c) son tres, siempre
con confirmación en dos pasos: aprobar una entrada pendiente, rechazarla y
reanudar el sistema tras una pausa por drawdown. Siguen existiendo los comandos de
terminal (`scripts.entries`, `scripts.system_control`) y hacen lo mismo.

**Aprobar una entrada se confirma con un deslizador (2026-10-15).** Primero "Aprobar
entrada" muestra el resumen y el aviso de que no se puede deshacer; después, en vez de
un segundo botón, hay que arrastrar el deslizador hasta el 100 %. Si lo sueltas antes,
no pasa nada, y "Cancelar" cierra el panel (al reabrirlo empieza en 0 %). Es solo
presentación: la acción, sus validaciones y los bloqueos (modo solo lectura, archivo
PARAR, pausa por drawdown) son los mismos. Rechazar entrada y reanudar el sistema siguen
con su botón "Confirmar" (`slide_to_confirm` en `app/ui/components/confirm_action.py`).

### Cómo abrirla

```
docker compose build              # una vez (y al cambiar dependencias): imagen compartida con `app`
docker compose up -d ui
```

Y abrir <http://localhost:8501>.

**Con doble clic (Windows, 2026-10-11):** `Abrir interfaz.bat`, en la raíz del proyecto
(`scripts\abrir_interfaz.ps1`). Se asegura de que Docker Desktop esté corriendo (lo
levanta y espera hasta 2 minutos), arranca SOLO la base de datos y la interfaz (no el
scheduler ni ningún ciclo de IA), espera a que responda y abre el navegador. Si la
interfaz ya estaba en marcha, solo abre el navegador. La primera vez puede tardar unos
minutos en construir la imagen. `Detener interfaz.bat` detiene únicamente la interfaz
(no toca la base de datos ni el scheduler). Para tenerlo en el escritorio: clic derecho
sobre `Abrir interfaz.bat` -> Mostrar más opciones -> Enviar a -> Escritorio (crear
acceso directo). Es un `.bat` y no un `.exe` a propósito: es texto que puedes leer, no
no necesita empaquetadores y evita crear un binario sin firmar (Smart App Control ya bloqueó otros binarios en esta máquina).

> **Nota:** la carpeta de páginas se llama `app/ui/paginas/`, no `pages/`, a
> propósito: Streamlit descubre sola cualquier carpeta `pages/` junto al archivo
> de entrada y, al recargar una URL como `/posiciones`, ejecutaría ese archivo
> suelto en vez de `app/ui/app.py` (pantalla en blanco). Hay un test que lo vigila.
 Para detenerla: `docker compose stop ui`.

### Qué muestra (ocho páginas)

Dashboard, Análisis (último ciclo), Posiciones, Operaciones (filtros + detalle
con la decisión congelada y la trayectoria), Performance (por periodo, símbolo y
tercil de score), Validación de estrategia (las seis preguntas de la sección
8.7 más el análisis por factor de 8.8; la pregunta 4, que necesita el backtest, dice `PENDIENTE`), Decisiones
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

- El pre-filtro se guarda por ciclo desde 2026-10-07 (`prefiltro_resultados`);
  en los ciclos anteriores no existe y sus "candidatos evaluados" se
  reconstruyen de las llamadas al Analista cuantitativo (la UI lo avisa).
- El análisis por factor (`factor_analysis`, sección 8.8) está en la página
  Validación (`app/domain/rules/factors.py`, `app/results/factor_analysis_service.py`).
  Por factor muestra: peso asignado, frecuencia (operaciones y % del total en que
  se puntuó), resultado promedio y win rate, confianza (conteo de etiquetas
  alta/media/baja del Consolidador + estado por tamaño de muestra) y contribución
  observada = expectancy del tercio alto menos la del bajo del puntaje del factor,
  con IC 95% por bootstrap (mismo método y semilla que el score). Es solo lectura
  y no interpreta: la lectura es del usuario. Con menos de 3 operaciones no hay
  terciles; con un puntaje idéntico en todas no se calcula contribución (el
  desempate por fecha inventaría una); son 4 comparaciones con IC 95% cada una
  (comparaciones múltiples, no corregidas). Hasta 30 operaciones cerradas el
  estado es `MUESTRA_INSUFICIENTE`/`PRELIMINAR`.
- No hay caché: cada interacción vuelve a leer la base (suficiente para uso
  personal; se añade si se vuelve lenta).

### Acceso remoto seguro con Tailscale (cliente web seguro, 2026-10-13)

Para ver la interfaz —y usar sus tres acciones (aprobar/rechazar entrada,
reanudar)— desde el celular o fuera del PC, **sin exponer nada a internet**. Es
una enmienda consciente a §21/§10 del plan (que piden no exponer en la fase
inicial): se resuelve con una **red privada**, no con un dominio público ni con
código de login. Decisión y alcance en `claude/decisiones-cliente-web-seguro.md`.

Por qué Tailscale y no OAuth/contraseña/TOTP en la app: cero código de
autenticación nuevo, cero dependencias nuevas, cero puerto o dominio público — la
menor superficie de ataque posible (§21, §31, §38). La identidad la pone tu
tailnet (tus dispositivos); la credencial de escritura **nunca sale de tu PC**.

Montaje (una vez):

1. Instala Tailscale en tu PC (donde corre Docker) y en tu celular; inicia sesión
   con la MISMA cuenta en los dos. Quedan en tu red privada.
2. En el panel de Tailscale, habilita **MagicDNS** y los **certificados HTTPS**
   (necesario para la URL `https://…ts.net`).
3. Deja la interfaz local corriendo (`Abrir interfaz.bat` o `docker compose up -d ui`;
   escucha en `127.0.0.1:8501`).
4. Publica esa interfaz SOLO dentro de tu tailnet, con TLS:

   ```powershell
   tailscale serve --bg 8501
   ```

   Imprime una URL del tipo `https://<tu-pc>.<tu-tailnet>.ts.net`, accesible solo
   desde tus dispositivos. Ábrela en el celular (con Tailscale activo) y tienes la
   interfaz con sus acciones.

Comandos útiles: `tailscale serve status` (ver qué está publicado) y
`tailscale serve off` (dejar de publicar).

**IMPORTANTE — nunca uses `tailscale funnel`.** `serve` es privado (solo tu
tailnet); `funnel` publicaría la interfaz a todo internet, justo lo que §21
prohíbe. Para este caso siempre `serve`.

Notas:

- Las tres acciones ya quedan registradas en las tablas operativas (la orden
  aprobada/rechazada, el evento de reanudación): hay rastro de auditoría sin
  código extra.
- Si la página carga pero se queda "conectando" (Streamlit tras el proxy), añade
  al arranque de la UI `--server.enableXsrfProtection=false` (aceptable porque la
  frontera de seguridad es la tailnet); normalmente no hace falta.
- La pila pesada que propusiste al inicio (OAuth de Google + contraseña + TOTP)
  queda para la **Fase 6** (capital real), como prerrequisito duro de esa fase y
  solo si entonces se decide que la tailnet no basta. Hoy, en paper, no se
  construye (§29).

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

## Despliegue en Railway — base de datos (Paso 11-0, 2026-10-07)

Decisiones en el documento del proyecto `claude/decisiones-paso-11.md`. La base
vive en el proyecto `trading-paper` de Railway (región `europe-west4`, la que
Binance acepta; las de EE. UU. están bloqueadas), con volumen persistente, TLS
activado y **sin dominio público**. Solo se expone el puerto de Postgres por un
proxy TCP; la contraseña la generó Railway y no está en el repositorio.

- Servidor: `iriguchi.proxy.rlwy.net:11001`, base `crypto_trading`, usuario `crypto`.
- TLS: certificado autofirmado generado una vez y guardado en el volumen. Su parte
  pública está en `docker/railway-db-server.crt` (no es secreto) y se usa con
  `sslmode=verify-ca`, así el cliente comprueba que habla con ESE servidor.
  (Huella SHA-256: `AE:A3:8F:AD:FC:39:7B:24:BC:E1:6D:AB:24:83:04:65:E9:EB:F7:BB:2C:40:D7:62:57:69:C1:4B:57:70:D0:D0`.)
- La URL tiene esta forma (la contraseña se copia del panel de Railway, variable
  `POSTGRES_PASSWORD` del servicio `timescaledb`; nunca se escribe en un archivo del repo):

```
postgresql+psycopg://crypto:<contraseña>@iriguchi.proxy.rlwy.net:11001/crypto_trading?sslmode=verify-ca&sslrootcert=/app/docker/railway-db-server.crt
```

Para apuntar UN comando a Railway sin guardar la contraseña (PowerShell; la variable
vive solo en esa ventana):

```powershell
$pw = Read-Host "Contraseña de la BD en Railway" -AsSecureString
$env:DATABASE_URL = "postgresql+psycopg://crypto:" + [System.Net.NetworkCredential]::new("", $pw).Password + "@iriguchi.proxy.rlwy.net:11001/crypto_trading?sslmode=verify-ca&sslrootcert=/app/docker/railway-db-server.crt"
docker compose run --rm -e DATABASE_URL app python -m scripts.db_summary
docker compose run --rm -e DATABASE_URL app alembic upgrade head
docker compose run --rm -e DATABASE_URL app python -m scripts.db_summary
Remove-Item Env:DATABASE_URL
```

Use `alembic upgrade head` (no `scripts.migrate_all`): en Railway no existe la base de
pruebas `crypto_trading_test`. `scripts.db_summary` es de solo lectura: muestra versión,
si hay TLS, la extensión TimescaleDB, la migración y el número exacto de filas por tabla
(sirve para comparar antes y después de restaurar tus datos).

### Rol de solo lectura para la interfaz (`crypto_ui`)

La interfaz que se conecte a Railway no necesita escribir nunca. En vez de confiar
solo en el código, usa un rol de PostgreSQL **sin privilegios de escritura**
(`app/repositories/db_roles.py`): `LOGIN`, sin superusuario, `SELECT` sobre las
tablas de `public` (y sobre las futuras), `default_transaction_read_only = on`.
Aunque alguien cambiara el código o inyectara SQL, la base lo rechazaría.
`tests/integration/test_db_roles.py` lo comprueba (INSERT, UPDATE, DELETE, TRUNCATE y
CREATE TABLE fallan con "permission denied", incluso desactivando el modo solo lectura).

Con ese rol la interfaz **detecta** que no puede escribir (mira el privilegio real,
`has_table_privilege`) y muestra **deshabilitados**, con la explicación, los botones
Aprobar entrada, Rechazar entrada, Reanudar el sistema, Iniciar/Rehacer la entrevista
y Guardar perfil. Las lecturas funcionan igual. La entrevista del Perfil queda
deshabilitada en este modo (decisión por defecto: escribe en `perfil_usuario`); las
acciones se hacen desde la interfaz local, conectada con el usuario de escritura
(y, desde 2026-10-13, también desde el celular/PC por Tailscale apuntando a esa
interfaz local — ver "Acceso remoto seguro con Tailscale").

Crear el rol (idempotente; repetirlo actualiza contraseña y permisos). La contraseña
del rol se genera en tu PC, vive solo en esa ventana y **debes guardarla en tu
gestor de contraseñas** antes de cerrarla (`Set-Clipboard` la copia al portapapeles):

```powershell
$pw = Read-Host "Contraseña de la BD en Railway (usuario crypto)" -AsSecureString
$admin = [System.Net.NetworkCredential]::new("", $pw).Password
$env:DATABASE_URL = "postgresql+psycopg://crypto:$admin@iriguchi.proxy.rlwy.net:11001/crypto_trading?sslmode=verify-ca&sslrootcert=/app/docker/railway-db-server.crt"
$bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$env:UI_DB_PASSWORD = -join ($bytes | ForEach-Object { $_.ToString('x2') })
Set-Clipboard $env:UI_DB_PASSWORD     # pégala YA en tu gestor de contraseñas
docker compose run --rm -e DATABASE_URL -e UI_DB_PASSWORD app python -m scripts.create_ui_role
```

Verificar que el rol funciona (lee todas las tablas, incluida la hipertabla `velas`) y
abrir la interfaz contra Railway en http://localhost:8502 (el servicio `ui-railway` no
arranca con un `up` normal, no recibe claves de Binance/Telegram/Anthropic y solo escucha
en `127.0.0.1`):

```powershell
$env:UI_DATABASE_URL = "postgresql+psycopg://crypto_ui:$env:UI_DB_PASSWORD@iriguchi.proxy.rlwy.net:11001/crypto_trading?sslmode=verify-ca&sslrootcert=/app/docker/railway-db-server.crt"
$env:DATABASE_URL = $env:UI_DATABASE_URL
docker compose run --rm -e DATABASE_URL app python -m scripts.db_summary
docker compose --profile railway up -d ui-railway
Remove-Item Env:DATABASE_URL, Env:UI_DB_PASSWORD, Env:UI_DATABASE_URL
```

(Si cierras la ventana sin guardar la contraseña, basta repetir `create_ui_role`: genera una nueva.)

### Sincronizar tus datos locales con Railway (`scripts.copy_database`)

Sincroniza **todas** las tablas de la base local con la de Railway. Es
**re-ejecutable** (tarea #68, 2026-10-13): no exige destino vacío y la puedes
correr cuantas veces quieras — cada corrida converge el destino hacia el origen.
No usa `pg_dump`/`pg_restore` porque `velas` es una hipertabla de TimescaleDB
(restaurarla así exige la misma versión de la extensión).

Estrategia por tabla (se decide sola):

- **Tablas inmutables** (las que tienen un trigger que rechaza cambios: velas
  cerradas, `trade_results`, `trade_snapshots`, `alertas`, `equity_observaciones`,
  `eventos_sistema`, `perfil_usuario`, `prefiltro_resultados`, `kronos_forecasts`,
  `propuestas`, `factores`): solo se agregan las filas nuevas (`ON CONFLICT DO NOTHING`).
- **Tablas mutables** (`posiciones`, `ordenes`, `performance_metrics`): se
  actualizan (`UPSERT`), para reflejar los cambios de estado.
- **`velas`**: solo las **cerradas**; la vela en curso entra cuando cierra.

Garantías (si algo falla, no se escribe nada):

- Las dos bases deben estar en la **misma migración** y tener las mismas tablas.
- Todo el destino se escribe en **una transacción** y solo se confirma si, al
  final, el destino es **superconjunto** del origen (ninguna fila del origen falta).
- Los ids se preservan (no se rompen las claves foráneas); las secuencias se reajustan.
- El origen se lee con una foto consistente (`REPEATABLE READ`).
- Sin `--ejecutar` solo informa (simulación, no escribe nada). Nunca imprime las URLs.

Limitaciones menores: el recibo de entrega de `alertas` y la vela en curso no se
reflejan en el espejo.

**Cuándo sincronizar:** cuando quieras un respaldo al día. Conviene con el monitor
y la tarea programada locales detenidos (o aceptando que la vela en curso y algún
estado muy reciente entren en la siguiente corrida). Puedes programarla tú con el
Programador de tareas de Windows.

```powershell
docker compose run --rm app alembic upgrade head          # la base local, en la última migración
$pw = Read-Host "Contraseña de la BD en Railway (usuario crypto)" -AsSecureString
$env:DEST_DATABASE_URL = "postgresql+psycopg://crypto:" + [System.Net.NetworkCredential]::new("", $pw).Password + "@iriguchi.proxy.rlwy.net:11001/crypto_trading?sslmode=verify-ca&sslrootcert=/app/docker/railway-db-server.crt"
$env:SOURCE_DATABASE_URL = "postgresql+psycopg://crypto:crypto@postgres:5432/crypto_trading"
docker compose run --rm -e SOURCE_DATABASE_URL -e DEST_DATABASE_URL app python -m scripts.copy_database             # simulación
docker compose run --rm -e SOURCE_DATABASE_URL -e DEST_DATABASE_URL app python -m scripts.copy_database --ejecutar # sincroniza
$env:DATABASE_URL = $env:DEST_DATABASE_URL
docker compose run --rm -e DATABASE_URL app python -m scripts.db_summary                                          # comparar conteos
Remove-Item Env:SOURCE_DATABASE_URL, Env:DEST_DATABASE_URL, Env:DATABASE_URL
```

#### Sincronización diaria sin ventana (20:00 hora de Windows)

> **SUSPENDIDA el 2026-11-10 (costo).** La memoria de la base en Railway (~2,5–3 GB
> permanentes, picos de 5–6 GB) duplicó la factura mensual (de 20 a 40,69 USD). Se
> desactivó la tarea (`Disable-ScheduledTask -TaskName CryptoTradingPlatform_SyncRailway`)
> y se quitó el despliegue de `timescaledb` en Railway (el volumen `timescaledb-data`
> se conserva). La base local es ahora la única copia. **Para reactivar:** Redeploy del
> servicio en el panel de Railway → `powershell -ExecutionPolicy Bypass -File scripts\sync_railway.ps1`
> (copia incremental, ~15 min) → `Enable-ScheduledTask -TaskName CryptoTradingPlatform_SyncRailway`.
> Mientras esté apagada, `ui-railway` no funciona. Decisión en `claude/decisiones-suspension-railway.md`.
> **Respaldo local (2026-11-10):** `pg_dump -Fc` de la base (1,4 GB → 110 MB) guardado en
> `F:\Respaldos_Trading\crypto_trading_20261110_2148.dump`, con SHA-256 verificado contra el
> original y `pg_restore --list` correcto. Para repetirlo: `docker exec crypto_trading_postgres pg_dump -U crypto -d crypto_trading -Fc -f /tmp/<archivo>.dump`
> y luego `docker cp` hacia F:\ (se puede hacer con el scheduler encendido). Restaurar en una base
> TimescaleDB nueva exige `SELECT timescaledb_pre_restore();` antes y `SELECT timescaledb_post_restore();`
> después del `pg_restore`. Ver «Respaldo local de la base» más abajo (scripts de respaldo programado y prueba de restauración).

Una sola vez, en PowerShell, en la carpeta del proyecto:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar_sync_railway.ps1
```

- Te pide la contraseña de la base de Railway y la guarda **cifrada con tu usuario de
  Windows (DPAPI)** en `%LOCALAPPDATA%\CryptoTradingPlatform\railway_db.cred`: fuera del
  proyecto (nunca llega a Git), ilegible para otro usuario u otro equipo, y no aparece en la
  tarea ni en ningún registro.
- Registra la tarea `CryptoTradingPlatform_SyncRailway`: cada día a las 20:00, sin ventana
  (`wscript //B` + `scripts\sync_railway_oculto.vbs` → `scripts\sync_railway.ps1`). Si Docker
  Desktop está cerrado lo inicia (espera hasta 180 s). Solo corre con tu sesión abierta. Si el
  equipo estaba apagado a las 20:00, **se pone al día** en cuanto Windows pueda (al encender e iniciar
  sesión; opción `StartWhenAvailable`, desde 2026-10-25) y lanza una sola copia aunque hayan sido
  varias las noches perdidas. Para aplicarlo a una tarea ya instalada, vuelve a correr el instalador
  (conserva la contraseña guardada).
- Al terminar hace una **simulación** (no escribe en Railway) para comprobar la conexión. Si dice
  que las migraciones son distintas, migra Railway una vez:
  `powershell -ExecutionPolicy Bypass -File scripts\sync_railway.ps1 -MigrarRailway`.
- Cada corrida escribe `logs\sync_railway_<fecha>.log`; la última exitosa deja
  `logs\sync_railway_ultimo_ok.txt`. Si falla (Docker, red, migraciones distintas, la copia
  aborta) avisa **una vez por Telegram** y termina con código 1.
- Comprobar: `(Get-ScheduledTaskInfo -TaskName CryptoTradingPlatform_SyncRailway).LastTaskResult`
  (0 = bien) y `Get-Content logs\sync_railway_ultimo_ok.txt`.
- Opciones: `-Hora 21:00`, `-SoloContrasena` (renovar la contraseña), `-Quitar` (borra la tarea;
  la contraseña cifrada se borra a mano). Manual: `scripts\sync_railway.ps1 [-Simulacion]`.

Tests: `tests/integration/test_copy_database.py` (primera sincronización con
identidades, simulación, re-ejecución idempotente, UPSERT propaga cambios de filas
mutables, velas solo cerradas, migraciones distintas, fallo a mitad = nada escrito).
Decisión en `claude/decisiones-copia-incremental-railway.md`.

## Respaldo local de la base (2026-11-10)

Desde que se suspendió la copia en Railway (ver arriba), el respaldo externo es un `pg_dump`
de la base local guardado en otra unidad. Solo LEE la base: se puede correr con el scheduler
encendido.

- **A mano:** `powershell -ExecutionPolicy Bypass -File scripts\respaldo_local.ps1`
  (opciones: `-Destino "F:\Respaldos_Trading"`, `-Conservar 8`). Hace `pg_dump -Fc` dentro del
  contenedor, lo copia al destino como `.partial`, compara el SHA-256 con el original, comprueba
  que `pg_restore --list` lo lee y solo entonces lo renombra a `crypto_trading_<fecha>_<hora>.dump`.
  Conserva los 8 más recientes (rota solo archivos `crypto_trading_*.dump` de esa carpeta). Si
  falla (unidad F: desconectada, Docker, hash distinto) no deja un `.dump` a medias, avisa UNA vez
  por Telegram y termina con código 1. Registro: `logs\respaldo_local_<fecha>.log`; éxito:
  `logs\respaldo_local_ultimo_ok.txt`.
- **Programado (una sola vez):** `powershell -ExecutionPolicy Bypass -File scripts\instalar_respaldo_local.ps1`
  registra `CryptoTradingPlatform_RespaldoLocal`: domingo 21:00 (hora de Windows), sin ventana, y
  se pone al día si el equipo estaba apagado (`-Dia`, `-Hora`, `-Quitar`). No guarda contraseñas.
  Comprobar: `(Get-ScheduledTaskInfo -TaskName CryptoTradingPlatform_RespaldoLocal).LastTaskResult`
  (0 = bien) y `Get-Content logs\respaldo_local_ultimo_ok.txt`.
- **Probar que sirve:** `powershell -ExecutionPolicy Bypass -File scripts\probar_restauracion.ps1`
  restaura el último respaldo en una base TEMPORAL `restore_test` (receta TimescaleDB:
  `CREATE EXTENSION` → `timescaledb_pre_restore()` → `pg_restore` → `timescaledb_post_restore()`),
  compara las filas de cada tabla con la base viva (solo lectura) y borra `restore_test`. Falla si
  una tabla falta, si la restaurada tiene MÁS filas que la viva o está vacía, o si cambia el
  número de hipertablas; las diferencias a favor de la viva (siguió escribiendo) se muestran y se
  aceptan. La base real nunca se escribe.
- **Restaurar de verdad** en una base nueva: crear la base, `CREATE EXTENSION timescaledb;`,
  `SELECT timescaledb_pre_restore();`, `pg_restore -U crypto -d <base> --no-owner <archivo>`,
  `SELECT timescaledb_post_restore();`.
- **Verificado el 2026-11-10:** `respaldo_local.ps1` terminó con código 0 (SHA-256 verificado,
  104,6 MB) y `probar_restauracion.ps1` terminó con «RESTAURACION VERIFICADA: todas las tablas e
  hipertablas coinciden». El contenido de los scripts se vigila además con
  `tests/unit/test_launcher_files.py` (incluye una prueba contra `"$variable: texto"`, error de
  PowerShell encontrado en la primera corrida).

## Avisos por Telegram (Paso 11a-1, 2026-10-07)

Decisiones en `claude/decisiones-paso-11.md` (D4, D5, D6, D8 y D9). Los avisos son
**solo avisos**: ninguno cambia una orden, una posición ni una regla de riesgo, y
Telegram es **solo saliente** (no hay comandos entrantes; aprobar y rechazar
siguen siendo manuales en la interfaz).

**Qué existe en 11a-1 (la infraestructura; los escalamientos están en la subsección de 11a-2):**

- Tabla `alertas` (migración `b7d41c9e2a60`): un aviso por fila, con el mensaje ya
  armado por código determinista, el valor observado frente al umbral y una clave
  de deduplicación `UNIQUE` por episodio (un reintento o un reinicio no duplica
  avisos). Un trigger impide borrar filas y cambiar el contenido de un aviso; solo
  se actualiza su **recibo de entrega** (`delivered_at`, `delivery_attempts`,
  `last_delivery_error`) y solo hacia adelante.
- `app/notifications/telegram.py`: cliente con `urllib` (cero dependencias nuevas).
  Texto plano, sin formato; el token nunca aparece en errores, logs ni `repr`.
- `app/notifications/dispatcher.py`: envía los avisos pendientes **después** de
  confirmar la transacción del chequeo, uno por transacción, con reintentos (máximo 5
  por aviso) y sin bloquear a los demás. Garantía "al menos una vez": si el proceso
  muere entre el envío y la anotación del recibo, ese aviso puede repetirse; no se
  pierde ninguno. Sin token configurado, los avisos esperan en la tabla.
- Capa nueva `app/notifications`, vigilada por `tests/unit/test_architecture.py`: la
  ejecución, el dominio, la IA, los servicios y la interfaz **no** la importan (P2), y
  nadie fuera de ella y los scripts lee el token (P4).

**Supuestos tomados por defecto (puedes cambiarlos antes de aplicar la migración):**
el recibo de entrega actualizable en la misma tabla, y el aviso de pausa por
drawdown además de los 5 escalamientos. Los cierres automáticos y las entradas
ejecutadas no avisan por ahora.

**Configurar Telegram (una sola vez):**

1. En Telegram abre a **@BotFather**, envía `/newbot`, elige nombre y usuario, y copia
   el token que te da.
2. Abre tu bot nuevo y envíale cualquier mensaje (por ejemplo "hola").
3. Obtén tu `chat_id` abriendo en el navegador
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (con tu token en lugar de
   `<TOKEN>`); busca `"chat":{"id":NÚMERO`. No pegues el token ni esa URL en ningún chat.
4. Escribe los dos valores en tu `.env` local (no se sube a git): `TELEGRAM_BOT_TOKEN=...`
   y `TELEGRAM_CHAT_ID=...`.
5. Prueba el envío: `docker compose run --rm app python -m scripts.telegram_test`.

Después de aplicar el parche hay que migrar las dos bases (real y de pruebas):
`docker compose run --rm app python -m scripts.migrate_all`. En Railway se aplica
con `alembic upgrade head` como en el despliegue de la base. Tests:
`tests/unit/test_telegram_client.py`, `tests/integration/test_alertas.py`.

### Escalamientos del monitor (Paso 11a-2, 2026-10-08)

Cada chequeo del monitor **escribe** en `alertas` los avisos que correspondan y,
una vez confirmada la transacción, `scripts/monitor.py` envía los pendientes por
Telegram. El monitor solo inserta filas (P2): enviar o no enviar nunca cambia una
orden, un stop ni una posición, y un fallo al avisar (o de Telegram) solo se
registra en el log. Las reglas son funciones puras en
`app/domain/rules/escalation.py`; el cableado con la base está en
`app/execution/escalations.py`.

| Aviso | Cuándo | Un aviso por |
|---|---|---|
| `CAIDA_1H` | una posición **abierta** cayó **más de** 5 % respecto del precio de hace 1 h (snapshot guardado) | posición y hora UTC |
| `VOLUMEN_ATIPICO` | volumen de la última vela 1 h cerrada a más de 3 desviaciones estándar de las 30 velas 1 h previas | posición y vela |
| `ORDEN_PENDIENTE_APROBACION` | hay una entrada esperando tu aprobación manual | orden |
| `BINANCE_CAIDO` / `BINANCE_RESTABLECIDO` | ningún precio durante más de 5 min / vuelve a llegar | episodio |
| `PAUSA_DRAWDOWN` | el equity llegó a -15 % del pico | pausa |
| `DISCREPANCIA_RECONCILIACION` | la reconciliación no cuadra y el monitor se detiene | hallazgos y hora UTC |
| `CICLO_DIARIO_PERDIDO` | el scheduler arrancó pasadas las 00:05 UTC y hoy no hay un ciclo de análisis completo (Paso 11c) | día UTC |
| `PRESUPUESTO_IA_AGOTADO` | el gasto de IA del día UTC alcanzó el tope diario y el ciclo automático no corrió | día UTC |
| `SALDO_IA_INSUFICIENTE` | el saldo estimado de Anthropic no alcanza para un ciclo y el ciclo automático no corrió | día UTC |
| `RESUMEN_CICLO` | un ciclo de análisis terminó (resumen de la corrida; ver abajo) | ciclo |
| `CICLO_FALLIDO` | un ciclo de análisis no terminó (solo el nombre del error) | instante del fallo |

**Resumen de cada corrida (decisión del usuario, 2026-10-11).** Al terminar cada
ciclo de análisis llega UN mensaje con: cómo se lanzó (programado 00:05 UTC,
disparado por candidatos nuevos, o manual), los candidatos evaluados, las
propuestas con su puntaje, tier y confianza (o, si no hubo, el motivo: el
pre-filtro no dejó candidatos, todos se excluyeron por calidad de datos, o el
Consolidador recomendó no invertir con su razón citada), el costo del ciclo y
el gasto de IA del día frente al tope. Si el ciclo falla llega `CICLO_FALLIDO`
con el NOMBRE del error (nunca su texto, que podría traer datos que no deben
viajar a un chat) y un recordatorio de revisar los logs.

- Cubre el ciclo del scheduler (diario y disparado por candidatos nuevos) y el
  manual (`scripts/run_daily_cycle.py`, también cuando lo lanza
  `run_daily_cycle.ps1`). El ciclo horario que no encuentra candidatos nuevos NO
  manda nada (sería un mensaje por hora sin información).
- Solo informa (P2): no cambia posiciones, órdenes ni reglas de riesgo, y un
  fallo al guardar o enviar el resumen nunca detiene el ciclo.
- Sin migración: `alertas.tipo` es VARCHAR sin restricción. Un resumen por
  ciclo (`dedup_key = resumen-ciclo:<ciclo_id>`): reintentar no lo duplica.
- Si Telegram no responde, el aviso queda pendiente y se reintenta (hasta 5
  veces) en el siguiente despacho.
- Código: `app/domain/rules/cycle_summary.py` (texto),
  `app/orchestration/cycle_summary.py` (guardado).

**Único efecto operativo:** mientras el último aviso del par Binance sea
`BINANCE_CAIDO`, `validate_entry` rechaza entradas nuevas con el motivo
`BINANCE_CAIDO` (como la pausa y `PARAR`). El monitor sigue vigilando y aplica los
stops en cuanto llega un precio.

**Derivados por el asistente (puedes corregirlos; están en `claude/decisiones-paso-11.md`):**

- Caída: estricta (exactamente 5 % no avisa). La referencia es el snapshot más
  reciente con hora ≤ ahora − 1 h y no más viejo que 5 min más; sin referencia (posición
  abierta hace menos de 1 h, o monitor apagado un rato) **no se avisa ni se estima**.
  Tras un reinicio, el aviso de caída vuelve a estar disponible 1 h después; los
  stops no dependen de esto.
- Volumen: desviación estándar muestral (n−1), z > 3 estricto. Faltan velas, no hay
  la última vela cerrada o la desviación es 0: no hay z-score y no se avisa. **Necesita
  velas de 1 h al día**: hoy se ingieren con el backfill; la ingesta horaria
  automática llega en 11b.
- Orden pendiente: se avisa de **toda** entrada que espera aprobación (el plan dice
  ">200 USD", pero en v1 todas miden 100 USD y la aprobación se exige también a las de
  mayor riesgo; con ">200" literal nunca avisaría).
- Binance caído: se detecta con los precios que el monitor pide para las posiciones
  abiertas y "último precio recibido" = último snapshot de esas posiciones. **Sin
  posiciones abiertas el monitor no llama a Binance y no puede saber si cayó**; la
  ingesta horaria (11b) cubrirá ese caso.
- Cada aviso de caída o de volumen deja además un snapshot `RISK_ALERT` en la
  trayectoria de la posición.

Tests: `tests/unit/test_escalation_rules.py`, `tests/integration/test_escalations.py`,
`tests/integration/test_monitor_avisos.py`. No hay migración nueva: usa la tabla
`alertas` de 11a-1.

## Vigilancia del scheduler (2026-10-12)

Los avisos anteriores salen DESDE el scheduler: si el propio scheduler se cae o
se congela, no hay quien avise. Esta vigilancia lo cubre con dos piezas que viven
fuera del scheduler, porque un proceso muerto no puede avisar de su propia muerte.
El scheduler ya escribía un latido (`.scheduler_heartbeat.json`) cada 5 minutos;
las dos piezas se apoyan en él.

**1. Vigía local (PowerShell, fuera de Docker) — `scripts/scheduler_watchdog.ps1`.**
Lee el archivo de latido y, si tiene más de 15 minutos o no existe, avisa una vez
por Telegram. Avisa de nuevo (recuperado) cuando el latido vuelve a estar fresco,
y no repite mientras el estado no cambie (guarda `.scheduler_watchdog_state.json`).
Como corre en Windows y no en Docker, detecta tanto que el proceso murió o se
congeló como que Docker se detuvo. Punto ciego: si el equipo está apagado o sin
energía, el Programador de tareas tampoco corre. No necesita Python; lee el token
de Telegram del `.env`. El umbral de 15 min es el mismo de la regla de dominio
(`app/domain/rules/heartbeat.py`, `UMBRAL_LATIDO_VENCIDO_MINUTOS`): si se cambia
uno, cambiar el otro.

Para dejarlo corriendo cada 5 minutos con el Programador de tareas de Windows
**sin que se vea ninguna ventana**, usa el instalador (una sola vez, en una consola
PowerShell en la carpeta del proyecto; si Windows pide permisos, ábrela como
administrador):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar_vigia.ps1
```

Es idempotente: si la tarea `VigiaScheduler` ya existía (por ejemplo una creada
antes con ventana visible) la reemplaza. Dos cosas la hacen invisible:
`-WindowStyle Hidden` y, sobre todo, registrar la tarea con `-LogonType S4U`
("ejecutar tanto si el usuario inició sesión como si no"), con lo que Windows la
corre en segundo plano, sin consola y sin guardar tu contraseña. Una tarea normal
que lanza `powershell.exe` parpadea una consola cada vez aunque se use
`-WindowStyle Hidden`, porque la ventana se crea antes de poder ocultarla. Para
quitarla: `powershell -ExecutionPolicy Bypass -File scripts\instalar_vigia.ps1 -Quitar`.
Si Windows responde «Acceso denegado» al registrar con S4U (pide administrador), el
instalador lo detecta y registra la tarea con `scripts\vigia_oculto.vbs`: `wscript.exe`
no crea consola y arranca PowerShell oculto, así que tampoco hay parpadeo y no hace
falta ser administrador; solo corre mientras tu sesión de Windows esté abierta. Con
`-SinAdmin` se salta directamente al lanzador. Para comprobar que corre: `Get-ScheduledTask -TaskName VigiaScheduler | Get-ScheduledTaskInfo`
(`LastTaskResult` debe ser 0).

Para probarlo de inmediato sin esperar: `powershell -ExecutionPolicy Bypass -File scripts\scheduler_watchdog.ps1`
con el scheduler apagado debe mandarte el aviso; con el scheduler encendido y un
latido fresco, no manda nada.

**2. Dead-man's-switch externo — `SCHEDULER_DEADMAN_URL` en `.env`.**
El scheduler hace un GET a esa URL en cada latido (cada 5 min). Si los pings
paran, el servicio externo avisa POR SU CUENTA; como vive fuera de tu máquina,
cubre el caso que el vigía local no puede: equipo apagado o sin energía. Avisa
con el retraso de su cuenta regresiva (p. ej. 15 min), por eso complementa al
vigía local, que avisa más rápido. El ping es best-effort (nunca tumba el
scheduler) y la URL nunca se registra (puede llevar un identificador secreto).

Para configurarlo con healthchecks.io (gratis): crea un check, pon su periodo en
5 min y su gracia en 10-15 min, copia la "ping URL" y ponla en
`SCHEDULER_DEADMAN_URL`. Sirve cualquier servicio equivalente, o un endpoint
propio en Railway, sin cambiar código. Reinicia el scheduler para que lea la
variable: `docker compose up -d --force-recreate scheduler`.

La decisión de montar ambos niveles está en `claude/decisiones-vigilancia-scheduler.md`.
Tests: `tests/unit/test_heartbeat_rule.py`, `tests/unit/test_deadman.py`, y los
casos de latido/ping en `tests/unit/test_scheduler.py`. El vigía de PowerShell no
tiene test automático (corre en otro entorno); su lógica es corta y refleja la
regla de dominio ya probada.

## Refresco de velas a las 9:30 a. m. de Colombia (2026-10-17)

**Problema.** El ciclo horario solo descarga las temporalidades que cierran a esa hora.
La vela de 12 h cierra a las 12:00 UTC (7:00 a. m. en Colombia); si el equipo está
apagado a las 12:05 UTC esa vela queda guardada **a medias** hasta el ciclo diario de
las 00:05 UTC. No dañaba el análisis (el ciclo diario la refresca antes de analizar),
pero sí disparaba la protección de los backtests («velas vencidas sin cerrar»).

**Solución (decisión del usuario).** Un quinto job del scheduler, `candle_refresh`, a las
**14:30 UTC = 9:30 a. m. de Colombia** (UTC-5, sin horario de verano): repasa las velas
de `1h`, `4h` y `12h` (ventana de 3 días, idempotente) y recalcula sus indicadores de
forma incremental. **Solo velas e indicadores**: no corre el pre-filtro, no detecta
candidatos nuevos, no dispara IA y no escribe en tablas operativas, así que la enmienda
E3 no cambia. Si el equipo duerme a esa hora y despierta hasta 3 h después, el job corre
una vez al despertar (`misfire_grace_time`); si el equipo está apagado (no dormido) a
las 9:30, no corre y el ciclo diario de las 00:05 UTC vuelve a cubrirlo. El ciclo horario
de las 12:05 UTC sigue existiendo y no cambia.

Para aplicar el cambio hay que **recrear el scheduler**:
`docker compose up -d --force-recreate scheduler`.

Código: `app/orchestration/hourly_cycle.py` (`run_candle_refresh`,
`REFRESCO_VELAS_HORA_UTC`), `app/orchestration/scheduler.py` (`_job_refresco_velas`).
Pruebas: `tests/unit/test_candle_refresh.py`.

### Recuperación de velas vencidas al arrancar (2026-10-17)

**Qué hace.** Unos 10 segundos después de que el scheduler arranca (o se recrea), un job
de **una sola ejecución** (`stale_candle_catchup`) busca velas guardadas a medias cuya hora
de cierre ya pasó y las repara: las vuelve a descargar de Binance desde la más antigua y
recalcula los indicadores de esa serie. Es el mismo camino de código que
`scripts/repair_partial_candles.py --ejecutar`, pero automático. Así, si el equipo estuvo
apagado en un cierre (p. ej. el de 12 h), la vela queda corregida en cuanto el equipo vuelve
a encenderse, sin esperar a las 9:30 ni al ciclo diario.

**Qué NO hace.** Solo velas e indicadores: no corre el pre-filtro, no detecta candidatos, no
llama a la IA y no escribe en tablas operativas. Solo atiende símbolos de la lista
autorizada. Corre en un hilo aparte: no retrasa al monitor de 60 s.

**Robustez.** Una transacción por serie: si Binance falla en una, se registra el error y se
sigue con las demás (lo ya reparado queda). Un fallo total se registra y no mata al
scheduler; el refresco de las 14:30 UTC y el ciclo diario vuelven a cubrirlo.

**Qué ve el usuario.** En el log del scheduler: `Recuperación de velas vencidas: series=…,
velas reparadas=…` (o «no hay velas vencidas sin cerrar»). Si hubo series fallidas o velas
sin reparar, el mensaje sale como aviso.

**Límite conocido.** Solo corre al *arrancar*. Si el equipo se duerme con el scheduler vivo y
despierta, la recuperación no se repite sola: lo cubre el refresco de las 9:30 (con 3 h de
gracia) y, si no, el ciclo diario.

Código: `app/orchestration/hourly_cycle.py` (`run_stale_candle_catchup`),
`app/orchestration/scheduler.py` (`_job_recuperar_velas_vencidas`). Pruebas:
`tests/unit/test_candle_refresh.py` y `tests/integration/test_stale_candle_catchup.py`.

### Guardia de velas vencidas de los estudios (ajuste 2026-10-17)

**Problema.** Cada hora entre :00 y :05 los scripts de estudio/backtest se negaban a correr
(«hay N vela(s) vencidas sin cerrar»): la vela de 1 h guardada a medias por el ciclo anterior
ya expiró y el ciclo de :05 aún no la refresca. También bloqueaban velas de 4 h/12 h que los
estudios no leen.

**Cambio.** Los scripts `backtest_walk_forward`, `backtest_diagnosticos`, `study_support_h1`,
`estudio_orden_intravela` y `estudio_velas_1m` usan ahora
`velas_vencidas_que_invalidan_un_estudio`: solo mira **1d y 1h** (lo que leen) e ignora lo
vencido hace **menos de 10 minutos**. Un estudio lee solo velas cerradas, así que una vela
recién expirada queda fuera de la serie como cola, nunca como hueco intermedio; un parcial
antiguo en el medio de la serie sigue bloqueando.

**Lo que NO cambia.** `velas_vencidas_sin_cerrar` conserva por defecto la definición estricta
(sin gracia, todas las temporalidades); la usan la reparación, la recuperación al arrancar y el
ciclo horario. Los valores (`TEMPORALIDADES_DE_ESTUDIO`, `GRACIA_DE_ESTUDIO`) están en
`app/data/candle_ingestion.py`. Pruebas: `tests/integration/test_guardia_de_estudio.py`.

## Cálculo incremental de indicadores en el ciclo horario (2026-10-12)

El ciclo horario recalculaba **toda** la historia de indicadores cada hora: por
cada símbolo cargaba sus ~39.000 velas 1 h, recomputaba los seis indicadores
sobre todas (~236.000 valores) y mandaba a Postgres un `INSERT ... ON CONFLICT DO
NOTHING` con todas esas filas, de las que casi todas ya existían. Por 20 símbolos
eran millones de filas por hora para, en la práctica, una vela nueva por símbolo.

Ahora el ciclo horario calcula solo sobre una **cola** de las últimas 1.000 velas
(`VELAS_DE_COLA_INCREMENTAL` en `app/data/indicator_pipeline.py`). Medido: el
cómputo por símbolo pasa de ~390 ms a ~7 ms (unas 55×), y se generan ~42× menos
filas. El ciclo diario (00:05) sigue recalculando toda la historia, porque es
quien mantiene la distancia-ATH (máximo expansivo sobre toda la serie).

Lo importante es que **los valores no cambian**: los indicadores de las velas
nuevas salen idénticos a los de historia completa, a la escala con que se guardan
(`NUMERIC(30,12)`). Los de ventana fija (SMA, volumen, distancia 90 d) son exactos
por construcción; los recursivos de Wilder (RSI, ATR) convergen al mismo valor
porque la cola trae ~928 velas de calentamiento para la vela nueva y bastan ~382
para los 12 decimales. La zona "fría" del inicio de la cola corresponde a velas ya
guardadas, que `ON CONFLICT DO NOTHING` nunca sobrescribe. Un test comprueba la
igualdad exacta (`tests/unit/test_indicator_incremental.py`); hay tests del
parámetro `tail` en `tests/unit/test_indicator_pipeline.py`, de `get_candles(tail=)`
en `tests/integration/test_candles_repository.py` y del camino incremental de punta
a punta en `tests/integration/test_indicator_pipeline_bulk.py`. Decisión en
`claude/decisiones-indicadores-incremental.md`.

## Apertura automática de posiciones paper y operaciones de observación (2026-10-12)

**Por qué.** Hasta el 2026-10-12 nada abría posiciones por sí solo: el scheduler
corría el ciclo y guardaba propuestas, y abrir una entrada exigía lanzar a mano
`scripts.entries open --ciclo-id`. Además la estrategia casi nunca produce una
entrada (exige puntaje ponderado ≥ 55 y el Consolidador suele recomendar no
invertir). Resultado: semanas de corridas, costo de API, **cero operaciones** y
ninguna información sobre si el score predice (que es el objetivo de la v1).

**Qué hace ahora.** Al terminar cada ciclo de análisis (el diario de las 00:05 y el
disparado por candidatos nuevos), el scheduler ejecuta una fase nueva,
`app/orchestration/entry_opening.py::abrir_entradas_del_ciclo`, siempre con las
mismas reglas de riesgo del código (`app/domain/rules/risk.py`):

1. **Estrategia** (sin cambios de regla): las propuestas del ciclo que calificaron.
   `NORMAL` (≥ 55) entra sola con 100 USD; `MAYOR_RIESGO` (50–55) queda pendiente
   de tu aprobación. Esto solo *conecta* lo decidido en el Paso 8.
2. **Observación** (nuevo): cada candidato del pre-filtro (estado `CANDIDATO`,
   puntaje ≥ 45, máximo 5 por ciclo) que la estrategia no abrió abre una posición
   paper de **100 USD**, **sin IA**, con stop −8 %, objetivo +12 % y trailing 6 %
   como cualquier otra. Su "score" es el del **pre-filtro**.

**Cómo se mantienen separadas (decisión del usuario):**

- **Cupos propios**: 20 observaciones abiertas y 5 por día (desde el 2026-10-26;
  antes 4 y 3), aparte de los de la estrategia (4 abiertas, 3 por día; sin cambios).
  Con 4 abiertas y sin salida por tiempo los cupos se llenaron el 16/10 y la serie
  dejó de crecer. Con 100 USD fijos, 20 abiertas son 2.000 USD nocionales fuera del
  equity. Las observaciones anteriores se abrieron con los topes viejos: al analizar
  la serie conviene separarla por la fecha del cambio. Para aplicarlo hay que
  **recrear el scheduler**. Un símbolo abierto en cualquier serie bloquea
  otra entrada en ese símbolo (índice único de la base).
- **Fuera del equity**: no cuentan para el equity, el pico ni la pausa por drawdown
  de −15 %, ni generan avisos de Telegram de escalamiento. El monitor sí las vigila
  y cierra (stop, objetivo, trailing).
- **Serie aparte al medir**: `scripts.results validate` imprime la validación de la
  estrategia, luego la de observación y luego una comparación conjunta (marcada
  como no válida porque mezcla dos escalas de puntaje). `trades` etiqueta cada
  operación; `performance --serie estrategia|observacion|todas` (solo la de
  estrategia se guarda en `performance_metrics`, que no tiene columna de serie). La
  interfaz muestra la serie en Operaciones, Posiciones, Decisiones, Performance y
  Validación.
- **La serie se deriva, no se duplica**: una operación es de observación si la
  propuesta que la originó tiene `tier_entrada = 'OBSERVACION'`
  (`app/repositories/series.py`). No hay columna nueva en posiciones ni órdenes.

**Trazabilidad (P3).** La fila de `propuestas` de una observación no inventa
análisis: tesis e invalidación son textos fijos que dicen que no hubo IA, no tiene
factores ni fuentes y `calidad_datos` queda en `NULL` (una restricción de la base
solo lo permite para el tier `OBSERVACION`). Su `decision_snapshot` congela lo que
el pre-filtro sabía ese ciclo (componentes del puntaje, versión de la fórmula, vela
base). Solo se guarda la propuesta de una observación que de verdad se abre (la
selección se hace primero en seco con la misma función pura que `open_entries`).

**Idempotencia (P5).** El id de la propuesta de observación es determinista (uuid5
de ciclo + símbolo) y el `client_order_id` de la orden sale de él: reintentar la
fase tras un reinicio no duplica propuesta, orden ni posición. Si la reconciliación
encuentra discrepancias, no se abre nada. Si la fase falla, el análisis ya quedó
guardado: el ciclo **no** se marca como fallido, el error se registra y el resumen
de Telegram lo dice.

**Resumen de Telegram.** Ahora incluye una sección "Posiciones paper" con cada
candidato: abierta, pendiente de tu aprobación, o no abierta con su motivo.

**Comandos.** `python -m scripts.entries open --ciclo-id <UUID>` usa la misma
función (estrategia + observación) y `list` etiqueta la serie de cada posición.
Aplica la migración nueva a las dos bases: `python -m scripts.migrate_all`
(`a7c2e5d1f9b3`: `propuestas.calidad_datos` admite `NULL` solo en observaciones).

**Limitaciones conocidas.**

- No hay salida por tiempo: una observación se queda abierta hasta que toca stop,
  objetivo o trailing; puede tardar y ocupar un cupo.
- Una observación abierta en el símbolo X impide una entrada de estrategia en X
  mientras siga abierta (se informa como `SIMBOLO_YA_ABIERTO`).
- Si el tope diario de IA o la guardia de saldo impiden iniciar el ciclo, no hay
  pre-filtro nuevo y por tanto tampoco observaciones ese día.
- El puntaje de observación y el de la estrategia son escalas distintas: no se
  comparan salvo en la vista conjunta, que es solo orientativa.
- Las observaciones sesgan la muestra hacia candidatos con tendencia y volumen
  (el pre-filtro). Medir si el score predice *dentro* de ese universo es válido;
  extrapolarlo a todo el mercado no.

Pruebas: `tests/unit/test_observation_rules.py`, `tests/integration/test_entry_opening.py`,
`tests/unit/test_cycle_summary_rules.py` y `tests/unit/test_scheduler.py`.


### Aviso de Telegram al abrir una posición (2026-10-12)

Cada vez que se abre una posición paper —estrategia automática, observación o
aprobación manual (UI o `scripts.entries approve`)— se deja una alerta
`POSICION_ABIERTA` en `alertas` y el despachador la envía a Telegram: símbolo,
origen, unidades, precio, total de compra, comisión, stop, objetivo y puntaje.
Es solo un aviso (P2): se escribe en un SAVEPOINT dentro de la misma transacción
de la entrada, tiene `dedup_key = posicion-abierta:<id>` (reintentar no lo
duplica) y si falla se registra y la entrada sigue. Se envía en cuanto el
monitor despacha pendientes (≤ 1 minuto). No requiere migración; las posiciones
abiertas antes de este cambio no reciben aviso retroactivo.

## Recuperación al arrancar: morir y revivir (Paso 11c, 2026-10-10)

El principio P5 pide que un reinicio, un corte de luz o de internet no deje
posiciones fantasma, órdenes duplicadas ni resultados inconsistentes. Casi todo
eso ya lo garantizaban las transacciones atómicas y los `client_order_id`
deterministas (Pasos 8 y 9); 11c cierra lo que faltaba al **arrancar el
scheduler** (`app/orchestration/lifecycle.py`):

1. **Un solo scheduler a la vez.** Antes de nada toma un candado de Postgres
   (`pg_try_advisory_lock`, `app/repositories/process_lock.py`) en una conexión
   propia. Si otro scheduler ya vive, sale con código 1 en lugar de duplicar
   ciclos y gasto de IA. Si el proceso muere (hasta con `kill -9`), la conexión
   se cae y Postgres libera el candado solo: no hay archivos que limpiar. Si ni
   siquiera puede consultar la base, también sale; Docker lo reintenta
   (`restart: unless-stopped`).
2. **Ciclos a medias.** Un ciclo de análisis sin `finished_at` es de un proceso
   que murió. Se marca como **interrumpido** (conserva su última etapa, se le
   pone fin y un mensaje, y su costo pasa a ser lo que realmente se gastó en
   IA según `llamadas_ia`). No se relanza ni se borra.
3. **Reconciliación.** Se reconcilia `posiciones` contra `ordenes`; si no
   cuadra queda un aviso `DISCREPANCIA_RECONCILIACION`. No corrige nada: el
   monitor y la apertura de entradas se quedan bloqueados (cada uno reconcilia
   antes de actuar) hasta que lo resuelvas a mano.
4. **Ciclo diario perdido.** Si ya pasaron las 00:05 UTC y hoy no hay un ciclo
   completo, avisa (`CICLO_DIARIO_PERDIDO`, una vez por día aunque se reinicie
   varias veces). **No lo relanza solo** (decisión del 2026-10-10): gastaría
   IA sin que lo pidas. Lánzalo a mano con `scripts/run_daily_cycle.py` si lo
   quieres.

   **Margen de recuperación (2026-10-26).** Si el equipo estaba suspendido a las
   00:05 UTC y el scheduler SIGUE VIVO (no se reinició), el ciclo diario corre una
   vez al despertar siempre que sea dentro de las **4 h** siguientes
   (`CICLO_DIARIO_GRACIA_SEGUNDOS`, `misfire_grace_time`). Antes el margen era el
   de APScheduler por defecto (1 s): el 26/10 el equipo despertó 21 s antes de las
   00:05, el ciclo no se lanzó y no hubo ningún aviso, porque el contenedor no se
   reinició. Pasadas las 4 h el ciclo ya no se recupera solo. Para aplicar el cambio
   hay que **recrear el scheduler**: `docker compose up -d --force-recreate scheduler`.

   **Ciclo horario (2026-10-29).** También se recupera: `CICLO_HORARIO_GRACIA_SEGUNDOS`
   = 1 h. Se pidió igualarlo a 4 h «o una mejor propuesta»: con `coalesce=True` solo se
   evalúa la ejecución perdida más reciente, que nunca pasa de 1 h de atraso, así que 1 h
   y 4 h se comportan igual (tras cualquier suspensión corre UNA vez al despertar, en
   lugar de esperar al siguiente minuto 5). El `_daily_cycle_lock` evita que un ciclo con
   IA lanzado por el horario coincida con el diario. Requiere recrear el scheduler.

Los pasos 2-4 corren cada uno en su transacción; si uno falla se registra y el
arranque sigue (nunca se deja de vigilar las posiciones abiertas). Los avisos se
envían a Telegram justo después.

**Qué se prueba** (`tests/integration/test_recovery.py`, contra Postgres real,
con sesiones nuevas por "proceso"): muerte antes de confirmar un stop (la
posición sigue entera, el reinicio vende **una** vez y al precio actual, un solo
`trade_result`), reintento tras TP parcial (no vende otra vez), entrada que
muere antes del commit (no deja posición fantasma), reintento de la misma
entrada tras reinicio (sin duplicados), ciclos huérfanos, aviso único del ciclo
perdido, bloqueo por discrepancia (monitor, abrir y aprobar) y el candado con un
`SIGKILL` real. Reglas puras: `tests/unit/test_recovery_rules.py`.

## Estudio offline de la hipótesis H1: soporte + RSI bajo (2026-10-08, protocolo v2)

Pregunta del usuario: ¿"precio cerca de un soporte + RSI de 4h en sobreventa" anticipa una recuperación? **No toca el score vivo, ningún prompt ni ninguna regla**: es un estudio **exploratorio** de solo lectura con protocolo pre-registrado en el repositorio (`docs/protocolo-h1-soporte.md`, v2), fijado antes de ver resultados. El script imprime la huella SHA-256 del protocolo para poder verificar que no cambió entre el commit y la corrida.

Compara dos definiciones de soporte: **S1** mínimo de oscilación (fractal diario de N=5, ventana de 180 velas) y **S2** mínimo de 90 días; condición = cierre diario entre el soporte y soporte × 1,02 **y** RSI-14 de la última vela de 4h cerrada < 35. Mide, con las barreras vigentes (stop −8 % / objetivo +12 %), cuántas veces se toca el objetivo primero frente a entrar sin condición.

Correcciones de la v2 (tras la revisión del usuario, antes de ver resultados):

1. **Línea base doble:** bruta (todos los días) y ajustada por moneda y mes calendario (estratos moneda-mes, ponderada por los episodios de la condicional; solo estratos con datos de ambos lados). Una definición debe tener el límite inferior del IC de 97,5 % > 0 frente a **ambas**.
2. **Purga en el corte 70/30:** en exploración se excluyen las observaciones cuyo desenlace cae en o después del corte (o no se resolvió); se imprime cuántas.
3. **Métrica secundaria (no decide):** retorno neto de la salida real simulada (50 % en +12 %, trailing 6 % al resto, comisión 0,1 % por lado), en `app/data/exit_simulation.py`.
4. **Veredicto por definición:** `PASA` / `NO_PASA` / `SIN_EVALUAR_MUESTRA_INSUFICIENTE` (< 30 episodios resueltos). Una definición sin muestra no se declara refutada: veredictos `H1_RESPALDADA`, `H1_NO_RESPALDADA`, `H1_NO_RESPALDADA_EN_LAS_EVALUADAS_OTRAS_SIN_EVALUAR` y `NO_CONCLUYENTE_MUESTRA_INSUFICIENTE`. Aun un `H1_RESPALDADA` solo justifica continuar la prueba en paper con datos nuevos; **no** añade la señal al score.

```bash
python -m scripts.repair_partial_candles --ejecutar   # requisito: historial sin velas vencidas sin cerrar
python -m scripts.study_support_h1                     # ~1-2 min con los 20 símbolos
```

Código: `app/domain/rules/support.py` (definiciones), `app/data/support_study.py` (estudio puro), `app/data/exit_simulation.py`, `scripts/study_support_h1.py`. Pruebas: `tests/unit/test_support_rules.py` (incluye la prueba de ausencia de look-ahead), `tests/unit/test_support_study.py` (incluye una prueba que ata los parámetros del código al texto del protocolo) y `tests/unit/test_exit_simulation.py`. Los parámetros no tienen opciones de línea de comandos a propósito: cambiarlos tras ver resultados invalida el estudio.

## Backtest walk-forward del score cuantitativo (Fase C, 2026-10-11)

Responde la pregunta central de la Fase 5 del plan con datos históricos y **costo cero**: ¿las entradas con score alto rinden mejor que las de score bajo, y se mantiene **fuera de la muestra**? Solo lectura: no escribe en la base, no llama a la IA y no cambia el score ni ninguna regla.

```bash
python -m scripts.backtest_walk_forward                                  # ventanas 12/3/3 meses, los 20 símbolos
python -m scripts.backtest_walk_forward --entrenamiento 9 --prueba 3 --paso 3
```

Requiere el backfill de ~2 años en 1h y 1d y el historial reparado (igual que el estudio H1).

**Decisiones del usuario (2026-10-11):** ventanas por defecto entrenamiento 12 m / prueba 3 m / paso 3 m (parámetros de análisis, no reglas de riesgo; si los cambias tras ver resultados es otra evaluación); solo el score **determinista** (la IA queda fuera: sin costo, sin look-ahead, reproducible; el resultado valida esa parte del score y nada más).

**Cómo funciona:** cada día elegible y cada moneda es una entrada hipotética a las 00:00 UTC con el score cuantitativo de producción (`calculate_quantitative_score`, mismos indicadores). El desenlace se simula con las reglas de salida del motor paper (stop -8 %, cierre 50 % en +12 %, trailing 6 %, comisión 0,1 % por lado). La medición es la del plan (8.7): tercio superior vs. inferior por score, IC 95 % por bootstrap (núcleo único `bootstrap_diferencia_de_terciles`, compartido con la validación de operaciones reales). El veredicto sale de la prueba **agregada** fuera de muestra: `DISCRIMINA` solo con ≥ 30 observaciones y límite inferior del IC > 0.

**Limitaciones que el informe imprime siempre:** (1) la fórmula tiene constantes fijas, así que el entrenamiento no calibra nada: se muestra como referencia dentro de la muestra para compararla con la de fuera; (2) los 180 días de calentada de la SMA 180 recortan el historial elegible (con 2 años caben ~2 pliegues de 12+3, no 4; con `--entrenamiento 9` caben ~3); (3) las observaciones de monedas distintas el mismo día y las de días seguidos se solapan, por lo que el IC es optimista; (4) las observaciones sin desenlace conocido se excluyen y se cuentan, nunca se rellenan; (5) no incluye IA ni eventos.

Sin look-ahead: el score del día D usa solo velas diarias cerradas antes de D (hay un test que recalcula con la serie recortada y exige el mismo score), el desenlace mira solo velas de 1h posteriores, y los datos de entrenamiento cuyo desenlace cae dentro de la ventana de prueba se **purgan**.

Código: `app/domain/rules/walk_forward.py` (reglas y veredicto), `app/data/backtest_study.py` (observaciones), `scripts/backtest_walk_forward.py`. Pruebas: `tests/unit/test_walk_forward_rules.py`, `tests/unit/test_backtest_study.py`, `tests/unit/test_backtest_walk_forward_script.py`.

### Diagnósticos exploratorios del backtest (2026-10-12)

El primer informe real dio `NO_DISCRIMINA` y, fuera de muestra, con la relación invertida (tercio alto peor que el bajo). Para entender por qué, sin tocar el score:

```bash
python -m scripts.backtest_diagnosticos        # varios minutos con los 20 símbolos
```

Cuatro diagnósticos, solo fuera de muestra: (1) **sesgo de censura**: cuántas posiciones sin cerrar hay por tercil y la medición repetida valorándolas al último precio; (2) **por factor**: el tercil con el sub-score de tendencia, volumen, distancia al máximo de 90 d y RSI por separado; (3) **rango dentro del día**: el score reemplazado por su percentil entre las monedas del mismo día (cada día aporta por igual a cada tercil, lo que separa el efecto del régimen de mercado; mínimo 10 monedas por día, parámetro de análisis declarado); (4) **ventanas 9/3/3**. Los intervalos son del 95 % sin corrección por comparaciones múltiples. **Son exploratorios: no se reajusta la fórmula con ellos** (plan 8.7); un cambio sería una estrategia nueva, a validar con datos no vistos.

Código: `app/domain/rules/walk_forward_diagnostics.py`, `scripts/backtest_diagnosticos.py`; `exit_simulation.retorno_marcado_a_mercado` comparte el recorrido con `simular_salida`. Pruebas: `tests/unit/test_walk_forward_diagnostics.py` y ampliaciones de `test_exit_simulation.py`, `test_backtest_study.py` y `test_backtest_walk_forward_script.py`.

### Sensibilidad al orden intravela (análisis nuevo, 2026-10-17)

La simulación de salida usa velas de 1 h y no sabe si, dentro de una vela, el máximo
o el mínimo ocurrió primero. Los resultados publicados suponen «máximo primero»
(el trailing sube con el `high` y luego se compara el `low`). La lectura inversa
(«mínimo primero») compara el `low` con el trailing del máximo anterior y solo
después sube el máximo. **Ninguna es siempre la pesimista**: si el `low` toca el
trailing viejo, la inversa sale más bajo; si solo toca el trailing nuevo, la
inversa no sale en esa vela y la posición sigue. Solo difieren las velas en las
que, tras el cierre parcial, el `high` supera el máximo previo **y** el `low`
cae por debajo del trailing nuevo (rango de la vela mayor que ~6 %).

```bash
python -m scripts.estudio_orden_intravela                    # los 20 símbolos, toda la muestra
python -m scripts.estudio_orden_intravela --desde 2025-03-19  # solo la época no vista
```

Solo lectura. Imprime cuántas entradas cambian, el efecto medio sobre el retorno
neto y si cambian la discriminación del score y el veredicto. El parámetro
`orden_intravela` de `simular_salida` / `observaciones_de_simbolo` vale por defecto
«máximo primero»: **ningún resultado previo ni regla viva cambia**. Código:
`app/data/exit_simulation.py`, `app/domain/rules/intravela.py`,
`scripts/estudio_orden_intravela.py`. Pruebas: `tests/unit/test_exit_intravela.py`,
`tests/unit/test_intravela_rules.py`, `tests/unit/test_estudio_orden_intravela_script.py`.

### Resolver las velas ambiguas con velas de 1 minuto (análisis nuevo, 2026-10-17)

Continuación del estudio anterior: en vez de acotar la realidad entre dos lecturas, se
**elimina la suposición solo donde importa**. `clase_ambiguedad` (en
`app/data/exit_simulation.py`) decide, con una condición exacta y no aproximada, qué velas
de 1 h tienen un desenlace que depende del orden dentro de la hora:

- `stop_y_objetivo` — antes del cierre parcial, la vela toca el stop **y** el objetivo (la
  regla de 1 h cuenta STOP). Esta clase no la cubría el estudio anterior: las dos lecturas
  daban lo mismo.
- `parcial_y_trailing` — la vela toca el objetivo y su rango bastaría para tocar también el
  trailing (la regla de 1 h no evalúa el resto de esa vela).
- `orden_trailing` — tras el cierre parcial, el máximo sube y el mínimo toca el trailing
  nuevo (el caso medido en el estudio anterior).

Para esas velas se descargan sus **60 velas de 1 minuto** (datos públicos de Binance,
solo lectura) y se recorren con exactamente las mismas reglas. Cada descarga se **valida**:
las 60 velas deben ser consecutivas y agregar exactamente la vela de 1 h (misma apertura,
máximo, mínimo y cierre). Si no, la vela se declara **sin resolver**, usa la regla de 1 h y
se cuenta en el informe: nada se inventa ni se rellena. No se guarda nada en la base de
datos ni se crea una temporalidad nueva: las velas de 1 minuto viven en una caché de
archivos (`data/cache_velas_1m/`, fuera de Git) que evita volver a descargar.

```bash
python -m scripts.estudio_velas_1m --solo-contar   # cuántas velas habría que bajar; no usa la red
python -m scripts.estudio_velas_1m                 # descarga (con caché) e informe completo
python -m scripts.estudio_velas_1m --desde 2025-03-19
```

El informe muestra la cobertura (cuántas velas ambiguas, de qué clase, cuántas resueltas y
cuántas no y por qué), **dónde cae el desenlace real** respecto de las dos lecturas de 1 h
(igual a una, intermedio, o fuera de ambas), el efecto sobre el retorno medio y la
discriminación del score con el veredicto walk-forward de las tres lecturas. Si algo quedó
sin resolver (p. ej. un corte de red) el código de salida es 2: se vuelve a correr y la
caché conserva lo ya descargado. Limitación declarada: dentro de un solo minuto se sigue
suponiendo «máximo primero»; el informe imprime el mayor rango de un minuto para que se vea
que es irrelevante. Sin `resolvedor` (el valor por defecto) **ningún resultado previo ni
regla viva cambia**. Código: `app/data/exit_simulation.py`, `app/data/minute_candles.py`,
`app/data/exchange_client.py` (`fetch_minute_candles_of_hour`), `app/domain/rules/intravela.py`
(`ubicar_resuelta`), `scripts/estudio_velas_1m.py`. Pruebas:
`tests/unit/test_resolucion_1m.py`, `tests/unit/test_minute_candles.py`,
`tests/unit/test_estudio_velas_1m_script.py`.

### Ampliar el historial hacia atrás y repetir el backtest (protocolo pre-registrado, 2026-10-12)

Tras el resultado negativo, la decisión fue comprobar si es propio de 2025-2026 usando datos que no se han visto. El protocolo completo (qué no cambia, cómo se leerá cada resultado) quedó escrito **antes** de bajar los datos en `claude/decisiones-walk-forward.md` del proyecto.

```bash
# 1) Descargar solo el tramo viejo (de hace ~5 años a hace ~2 años); 1h y 1d bastan para el backtest
python -m scripts.backfill_candles --days 1825 --hasta-dias 740 --timeframes 1h,1d
# 2) Evaluación PRIMARIA: solo decisiones anteriores a 2025-03-19 (época no vista)
python -m scripts.backtest_walk_forward --hasta-fecha 2025-03-19
python -m scripts.backtest_walk_forward --hasta-fecha 2025-03-19 --entrenamiento 9
python -m scripts.backtest_diagnosticos --hasta-fecha 2025-03-19
# 3) Evaluación SECUNDARIA: toda la muestra
python -m scripts.backtest_walk_forward
```

`--days` es hasta dónde retroceder y `--hasta-dias` dónde parar, ambos desde hoy (`--hasta-dias` debe ser menor que `--days`); las monedas listadas después de 2021 simplemente tendrán menos historia. Efecto secundario: `DISTANCIA_ATH` (máximo sobre todo lo almacenado) sube para monedas cuyo máximo de 2021 fue mayor; no entra al score ni al pre-filtro, pero los agentes la ven como contexto. La fórmula **no se modifica** con estos datos.

## Experimento Kronos: señal competidora (enmienda E6, 2026-10-15)

**Qué es.** [Kronos](https://github.com/shiyu-coder/Kronos) es un modelo de
fundación para velas (MIT). Se prueba como *otra señal* contra la que contrastar
el score determinista. **No entra en compras, score, riesgo ni ejecución** y no
modifica el plan: vive aislado en `app/experiments/` y en su propia imagen Docker.
El plan completo y las decisiones aprobadas están en el documento del proyecto
`plan-kronos-senal-competidora.md`.

**Decisiones aprobadas (2026-10-15).** Modelo `Kronos-small` (24,7 M) · fecha de
corte **2024-07-01** (Kronos se preentrenó con datos de Binance hasta junio de
2024: nada anterior mide predicción) · lookback 400 velas de 1 h, horizonte 24
velas · solo OHLC (el preentrenamiento de cripto no tuvo volumen) · CPU (las GPU
AMD del equipo no tienen soporte ROCm en Docker/WSL2).

**Fases.** K0 viabilidad (hecho) → K1 backtest pre-registrado (en curso) → K2 sombra hacia
adelante → K3 lectura. Pasar de una a otra exige fijar antes los parámetros y los
criterios; usarlo en producción exigiría una enmienda nueva.

**K0 — qué hace y qué NO hace.** Mide, en este equipo, cuánto tarda un pronóstico,
si es reproducible con semilla y qué fracción de contextos se descarta por falta
de velas. Añade un chequeo de cordura contra "no cambia". **No saca conclusiones
de rendimiento** (pocas fechas, monedas correlacionadas).

**Seguridad (P2/P4).** Código de terceros: se fija por *commit* en el `Dockerfile`;
los pesos se fijan por revisión + SHA-256 en `data/kronos/pesos.lock.json` (se
versiona) y después se usan **sin red** (`HF_HUB_OFFLINE=1`). El contenedor no
recibe claves de Binance, Anthropic ni Telegram; la herramienta abre la base en
transacción `READ ONLY` y no escribe nada. Una prueba de arquitectura impide que
cualquier capa de `app/` importe `app.experiments`.

**Cómo ejecutarlo** (PowerShell, en la carpeta del proyecto):

```powershell
docker compose up -d                                   # la base debe estar arriba
docker compose --profile kronos build kronos           # una vez (descarga PyTorch CPU)
docker compose --profile kronos run --rm kronos python -m scripts.kronos_k0 --descargar
docker compose --profile kronos run --rm kronos python -m scripts.kronos_k0 --fechas 12 --trayectorias 8
```

El segundo paso fija los pesos (único con red) y escribe `data/kronos/pesos.lock.json`;
el tercero mide y guarda `data/kronos/k0-<fecha>.json`. Comprobación del cableado
con un modelo diminuto de pesos aleatorios (sin red ni pesos reales):

```powershell
docker compose --profile kronos run --rm kronos python -m pytest tests/integration/test_kronos_adapter.py
```

Los parámetros de K0 por defecto (8 trayectorias, T 1.0, top_p 0.9) son los mismos
que luego se congelaron para K1.

**K0 — resultado (2026-10-15).** Viable: carga sin red desde pesos fijados, es
reproducible con la semilla, tarda ~16 s por pronóstico de 8 trayectorias en CPU y
el ranking de monedas es estable entre muestreos (correlación de rangos 0,94). Un
hallazgo corregido: con un lote compartido el pronóstico de una moneda cambiaba
según sus acompañantes; ahora cada pronóstico usa una semilla derivada de
(semilla base, símbolo, fecha) y se calcula solo.

### K1 — backtest histórico pre-registrado

Protocolo congelado: `docs/protocolo-k1-kronos.md` (v1, aprobado 2026-10-15).
Parámetros fijos en el código (no son argumentos): 8 trayectorias, lookback 400,
horizonte 24, T 1.0, top_p 0.9, semilla 20261015; fechas de decisión desde 2024-07-01
cada 3 días (~279 fechas × 20 monedas ≈ 5.600 pronósticos ≈ 25 h de CPU) más un
diagnóstico pre-corte (cada 15 días entre 2023-07 y 2024-06).

```powershell
# 1) Una vez: crear la tabla (en tu base real)
docker compose run --rm app alembic upgrade head

# 2) Generar (reanudable; repite el MISMO comando hasta que diga «Generación completa»)
docker compose --profile kronos run --rm kronos python -m scripts.kronos_k1_generar --max-minutos 480

# 3) Evaluar (solo lee; se puede lanzar en cualquier momento, pero con la generación
#    incompleta el resultado es PARCIAL y no es el veredicto del protocolo)
docker compose --profile kronos run --rm kronos python -m scripts.kronos_k1_evaluar
```

- La primera vez, `kronos_k1_generar` fija las fechas en `data/kronos/k1_fechas.json`
  junto con la huella SHA-256 del protocolo. **Versiona ese archivo.** Si el protocolo
  se modifica después, el generador se niega a continuar (sería otro experimento).
- Cada pronóstico se guarda al calcularse en `kronos_forecasts` (solo-agregar; sin
  claves foráneas hacia tablas operativas). Si cortas el proceso, no se pierde lo hecho.
- `kronos_k1_evaluar` imprime el RankIC diario de Kronos y de las líneas base (score v1,
  momentum 24 h y 7 d) sobre las mismas fechas y monedas, el IC95 por bloques de fechas,
  la prueba de permutación, los terciles de §8.7 y la versión E4, y el veredicto del
  protocolo (`NO_CONCLUYENTE`, `NO_DISCRIMINA`, `DISCRIMINA_NO_AÑADE` o
  `DISCRIMINA_Y_AÑADE`). Incluso el mejor veredicto solo justifica pasar a K2 (sombra
  hacia delante): **nada de esto entra en compras, score ni pre-filtro.**
- Reiniciar el experimento a propósito (por ejemplo, si cambias los pesos): `TRUNCATE
  kronos_forecasts` (un trigger impide UPDATE y DELETE fila a fila).

**Generar por las noches, sin ventana (opcional).** La generación completa son unas 28 h
(incluye el diagnóstico pre-corte) y cada tanda manual se detiene a las 8 h. Para que se
lance sola de lunes a viernes a las 00:00 (hora de Windows), una vez:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar_k1_noche.ps1
```

Cada tanda (`scripts\k1_generar_noche.ps1`, sin ventana) abre Docker Desktop si hace falta,
**no arranca si ya hay un contenedor de Kronos corriendo** (p. ej. una tanda manual), pide a
Windows que no suspenda el equipo mientras corre y guarda su salida en
`logs\kronos_k1_<fecha>.log`. Si el equipo estaba apagado a las 00:00, esa noche se omite.
Cuando el generador diga «Generación completa» las tandas siguientes terminan en segundos;
quita la tarea con `... instalar_k1_noche.ps1 -Quitar`. Comprobar:
`(Get-ScheduledTaskInfo -TaskName CryptoTradingPlatform_K1_Noche).NextRunTime` y
`.LastTaskResult` (0 = bien). Opciones: `-Hora HH:mm`, `-MaxMinutos N`.

**Qué no hace todavía:** K2 (sombra hacia delante), K3 (lectura final) ni una página en la
interfaz.

## Métrica a la sombra del análisis de IA (S1, 2026-10-29)

Pregunta: entre los candidatos que el pre-filtro entrega a la IA en un mismo ciclo, ¿los que
el Consolidador **incluye** en sus opciones tienen un desenlace simulado mejor que los que
**no incluye**? Es un estudio de **sombra** y de **solo lectura**: no abre posiciones, no
escribe en la base, no llama a la IA y no cambia el score, el pre-filtro ni ninguna regla de
riesgo. El protocolo está congelado en `docs/protocolo-metrica-sombra-ia.md` (huella SHA-256
en `data/sombra_ia/protocolo_sha256.txt`; el script se niega a trabajar si el texto cambia).

```powershell
docker compose run --rm app python -m scripts.estudio_sombra_ia
```

- **Modo conteo** (mientras no haya 30 días con ciclo pareado, 30 ciclos pareados y 30
  candidatos incluidos): imprime cuánta muestra hay y cuánto falta. **No lee velas
  posteriores ni calcula ningún desenlace** (una prueba lo garantiza).
- **Evaluación**: se corre una sola vez al cumplirse los mínimos (`evaluacion-primaria-*.json`);
  las corridas siguientes se guardan como `actualizacion-*.json` y no la sustituyen.
- Limitación declarada: «omitido por el Consolidador» y «excluido por calidad de datos» no se
  pueden separar hoy (ambos quedan sin propuesta).
- Código: reglas puras en `app/domain/rules/sombra_ia.py`, desenlaces en
  `app/experiments/sombra_ia.py`, lectura en `app/repositories/sombra_ia.py`.


## Estudio D1: distancia a la media de 200 periodos (4 h) en BTCUSDT (2026-11-17)

Pregunta del usuario: ¿el precio de BTC oscila de forma acotada alrededor de la media de 200 periodos de 4 h (hasta ~+19 % por encima y ~−25 % por debajo), y estar en un extremo anticipa algo? **Estudio exploratorio de solo lectura** con protocolo pre-registrado y congelado (`docs/protocolo-distancia-ma200-4h.md`, v1; huella en `data/distancia_ma200/protocolo_sha256.txt`). **No toca el score, el pre-filtro, los prompts ni ninguna regla de riesgo**; incorporar la distancia como indicador exigiría una enmienda nueva.

- **Datos:** velas de 1 h cerradas de BTCUSDT agrupadas a 4 h (bloques UTC); un bloque con una hora faltante no existe y se cuenta. SMA primaria y EMA de comprobación, desde la vela 600 de cada tramo continuo.
- **Parte A (descripción):** distribución de la distancia (total y por año), cuántas veces y cuándo se superaron +19 % y −25 %, y qué fracción de las ventanas móviles de 365 días no contiene ninguna ruptura.
- **Parte B (predicción):** percentil móvil con datos solo pasados (ventana creciente, calentada de 1.080 velas); colas ≤ 5 y ≥ 95 frente al centro; retorno bruto a 18 velas (3 días) como contraste primario (hipótesis de reversión a la media), 6 y 42 velas, EMA, mitades y umbrales fijos como secundarios exploratorios. IC95 por bootstrap de bloques de 14 días (1.000 iteraciones, semilla 20261005); mínimo de 10 episodios por cola.
- **Controles de validez:** la vela de 4 h agregada debe coincidir exactamente con la de 4 h guardada; sin información futura (pruebas que truncan y alteran las velas posteriores).

```powershell
docker compose run --rm app python -m scripts.estudio_distancia_ma200 --solo-contar   # cobertura y control; sin distancias ni retornos
docker compose run --rm app python -m scripts.estudio_distancia_ma200                  # evaluación
```

Solo lee (transacción READ ONLY), sin IA ni red. La primera evaluación se guarda como `evaluacion-primaria-*.json` y las siguientes como `actualizacion-*.json`. Código de salida 2 si el control de agregación falla o no hay datos; 3 si el protocolo cambió. Pruebas: `tests/unit/test_distancia_ma.py` y `tests/unit/test_estudio_distancia_ma200.py`. Calibración informal con 30 paseos aleatorios sintéticos: 4 de 60 contrastes primarios salieron «en la dirección esperada» por azar (≈ 6,7 % frente al 2,5 % nominal), así que el intervalo es algo optimista; es una de las cautelas del protocolo.


**Resultado (evaluación primaria del 2026-11-17, `data/distancia_ma200/evaluacion-primaria-20261117-1709.json`).** Control de agregación cumplido (4.734 de 4.734 velas). 11.168 velas de 4 h (2021-10-13 → 2026-11-17), 9.970 observaciones elegibles, 8.890 clasificables. **Parte A:** la distancia a la SMA 200 de 4 h tuvo P5 −14,4 % y P95 +16,4 % (mín −36,3 %, máx +33,6 %); superó +19 % en el 3,4 % de las velas (11 episodios) y bajó de −25 % en el 0,5 % (3 episodios: mayo y junio de 2022 y 5-6 de febrero de 2026); solo el 5,2 % de las ventanas móviles de un año no contiene ninguna ruptura de [−25 %, +19 %] (la ventana limpia fue del 26/11/2024 al 4/2/2026); el P95 anual varió entre +9,9 % (2022) y +24,9 % (2024). **Parte B:** los dos contrastes primarios (SMA, retorno a 3 días) dieron `SIN_EVIDENCIA` (cola baja +0,06 pp [−1,35; +2,63]; cola alta +1,05 pp [−0,12; +2,32], signo contrario a la hipótesis de reversión), con 11 y 12 episodios. Los secundarios son exploratorios: dos con EMA rozaron `EN_CONTRA` (límite inferior +0,07 y +0,01 pp) y los umbrales fijos y las mitades no alcanzan los 10 episodios. Lectura completa en `claude/resultados-distancia-ma200-4h.md` del proyecto. No se cambió nada de la plataforma.

## Tarea diaria programada (Windows)

> **Estado (2026-10-15): tarea DESACTIVADA, reemplazada por el scheduler.** Desde el Paso 11b
> el ciclo diario lo corre el servicio `scheduler` de Docker a las 00:05 UTC (19:05 en
> Colombia), con tope diario de IA, guardia de saldo, candado y avisos. Esta tarea de Windows
> (10 AM) es anterior, no respeta esas guardias y, con las dos activas, había dos ciclos al
> día (≈1 USD/día en lugar de ≈0,5). Se dejó registrada pero desactivada
> (`Disable-ScheduledTask -TaskName CryptoTradingPlatform_DailyCycle`; reversible con
> `Enable-ScheduledTask`). Úsala solo si el PC no está encendido a las 19:05; el script
> `scripts\run_daily_cycle.ps1` sigue sirviendo para correr un ciclo a mano.

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
├── experiments/
│   └── kronos/            # E6: contexto sin look-ahead, pesos fijados, adaptador y K0 (aislado, ver sección Kronos)
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
│   ├── paginas/            # una página por módulo (`render()`); NO se llama `pages/` (ver nota)
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
│   └── analysis.py         # persistencia de ciclos_analisis/propuestas/factores/prefiltro_resultados
migrations/                # Alembic
scripts/
├── results.py                    # CLI: trades / performance / validate (Paso 9)
├── backfill_candles.py          # CLI: backfill histórico completo (velas en curso vs. cerradas)
├── repair_partial_candles.py    # CLI: repara velas congeladas a medias (simulación por defecto)
├── study_support_h1.py          # CLI: estudio offline H1 (soporte + RSI 4h), protocolo v2 en docs/
├── backtest_walk_forward.py     # CLI: backtest walk-forward del score cuantitativo (Fase C)
├── backtest_diagnosticos.py     # CLI: diagnósticos exploratorios del backtest (censura, factores, rango/día, 9/3/3)
├── estudio_orden_intravela.py   # CLI: sensibilidad del backtest al orden máximo/mínimo dentro de la vela de 1 h
├── estudio_velas_1m.py          # CLI: resuelve las velas ambiguas de 1 h con velas de 1 minuto (caché de archivos)
├── calculate_indicators.py      # CLI: cálculo masivo de indicadores
├── verify_indicators.py         # CLI: verificación de integridad de indicadores
├── seed_events.py                # CLI: carga/actualiza el catálogo de eventos
├── calculate_event_analysis.py  # CLI: cálculo masivo de analisis_eventos
├── run_prefilter.py              # CLI: puntúa los 20 símbolos y muestra los candidatos del día
├── run_agent_demo.py             # CLI: un agente, datos de ejemplo — consume API de pago
├── run_agent_demo_full_cycle.py  # CLI: 4 agentes, un símbolo real, sin persistir — consume API de pago
├── run_daily_cycle.py            # CLI: ciclo real completo, persiste todo — consume API de pago; explica el "sin propuestas"
├── run_daily_pipeline.py         # CLI: encadena backfill (--days 5) + indicadores + run_daily_cycle
├── instalar_k1_noche.ps1         # Registra la tarea nocturna (lun-vie 00:00) de la generación de K1, sin ventana
├── k1_generar_noche.ps1          # Una tanda nocturna de K1 (Docker listo, sin duplicar generador, sin suspender)
├── k1_noche_oculto.vbs           # Lanzador sin ventana de esa tanda
├── instalar_vigia.ps1            # Registra la tarea de Windows del vigía, sin ventana (S4U o lanzador .vbs)
├── vigia_oculto.vbs              # Lanzador sin ventana del vigía (alternativa que no pide administrador)
└── scheduler_watchdog.ps1        # Vigía local (PowerShell, fuera de Docker): avisa por Telegram si el scheduler no da señales
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
- Backtest walk-forward del score cuantitativo determinista (Fase C): `python -m scripts.backtest_walk_forward`. Ver "Backtest walk-forward". Todavía NO valida la parte de IA ni el contexto de eventos.
- Apertura automática de posiciones paper al terminar cada ciclo de análisis y
  serie de operaciones de **observación** medida aparte (2026-10-12; ver la
  sección "Apertura automática de posiciones paper y operaciones de observación").

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
- Monitor como proceso permanente y programado (APScheduler, Paso 11b): hoy
  el monitor se lanza a mano con `scripts.monitor --loop` y solo vigila
  mientras corre (los avisos de escalamiento por Telegram ya existen: Paso 11a-2). Ninguna credencial de
  Binance se usa todavía; ni el cliente de velas, ni el backfill, ni el monitor
  la necesitan.
- Corrección auditable de un `trade_result` y alcance "estrategia" de las
  métricas (ver "Resultados, snapshots y métricas"), y los snapshots
  `RISK_ALERT` (Paso 11).
- Ajuste de pesos o umbrales con los resultados: el plan lo prohíbe sin
  registrar antes una nueva versión de estrategia (sección 8.7).
- Desde la interfaz no se puede abrir un ciclo, cerrar una posición a mano, crear/borrar
  el archivo `PARAR` ni cambiar ninguna regla de riesgo (fuera de alcance de 10c).
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
