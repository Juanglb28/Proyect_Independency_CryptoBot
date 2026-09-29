# Catálogo de eventos macro

Formato de archivo consumido por `scripts/seed_events.py` (sección 3 del
plan: "Catálogo manual curado de 40-60 eventos fechados"). Ningún evento se
inventa en el código — este directorio es la única fuente de verdad de qué
eventos existen.

Cada `*.json` en este directorio es un array de eventos:

```json
[
  {
    "external_id": "btc-halving-2024",
    "event_date": "2024-04-20",
    "event_type": "HALVING",
    "description": "Cuarto halving de Bitcoin: la recompensa por bloque baja de 6.25 a 3.125 BTC.",
    "severity": "alta",
    "source": "https://www.blockchain.com/explorer/blocks/btc/840000",
    "scope": "BTCUSDT"
  }
]
```

## Campos

| Campo | Obligatorio | Descripción |
|---|---|---|
| `external_id` | Sí | Slug único y estable (minúsculas, guiones). Es la clave de idempotencia: correr `seed_events.py` dos veces con el mismo `external_id` actualiza el evento, no lo duplica. Nunca lo cambies una vez cargado — sería un evento nuevo, no una corrección. |
| `event_date` | Sí | Fecha exacta, `YYYY-MM-DD`. |
| `event_type` | Sí | Categoría libre (ver convención abajo). |
| `description` | Sí | Descripción factual, en texto plano. Este texto lo lee un agente de IA como dato, nunca como instrucción (sección P4 del plan) — aun así, que sea descriptivo y neutro. |
| `severity` | No | Convención: `alta`, `media`, `baja` (mismo vocabulario que el resto del plan para confianza/semejanza). |
| `source` | No | URL o cita verificable. Fuertemente recomendado — sin fuente, un evento no es auditable. |
| `scope` | No | `"GLOBAL"` (o campo ausente) si afecta a todo el universo de la whitelist; si no, símbolos de `app/domain/symbols.py::ALLOWED_SYMBOLS` separados por coma (p. ej. `"ETHUSDT"` o `"BTCUSDT,ETHUSDT"`). Un símbolo fuera de la whitelist hace fallar la carga. |

## Convención de `event_type` (propuesta, no forzada por la base de datos)

`REGULACION`, `MACROECONOMICO`, `HACKEO_EXCHANGE`, `QUIEBRA_EXCHANGE`,
`HALVING`, `APROBACION_ETF`, `ADOPCION_INSTITUCIONAL`, `EVENTO_RED` (upgrade
o hard fork), `CRISIS_MERCADO`, `OTRO`. Es una convención documentada, no un
enum de base de datos (mismo criterio que `indicadores`: no comprometerse a
categorías fijas antes de tener el catálogo real completo).

## Limitación conocida (no un bug)

`analisis_eventos` (el comportamiento del precio alrededor de cada evento)
solo puede calcularse para eventos dentro de la ventana de velas
efectivamente ingerida (~2 años, sección 3 del plan). Un evento más antiguo
queda igualmente en el catálogo — es contenido histórico válido por sí
mismo, y algunos de los 6 agentes puede razonar solo con la descripción —
pero `scripts/calculate_event_analysis.py` lo reportará como "sin velas" en
vez de inventar un número.

## Validar sin escribir en la base de datos

```bash
python -m scripts.seed_events --dry-run
```
