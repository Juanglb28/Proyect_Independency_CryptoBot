"""Carga el catálogo de eventos macro en la base de datos (sección 15.10 del
plan, Paso 5).

Lee todos los archivos `data/events/*.json` (cada uno un array JSON de
eventos, ver `data/events/README.md` para el formato exacto) y los guarda
en la tabla `eventos` con `save_events` — que hace upsert por
`external_id`, así que corregir un evento en el archivo y volver a correr
este script aplica la corrección en vez de duplicarlo.

Uso:
    python -m scripts.seed_events
    python -m scripts.seed_events --dry-run   # valida el catálogo sin tocar la base de datos

Este script NO decide qué eventos existen — el contenido del catálogo vive
en `data/events/*.json`, curado manualmente (sección 3 del plan: "Catálogo
manual curado de 40-60 eventos fechados"). Aquí solo se valida la forma
(campos obligatorios, fechas, símbolos de `scope` dentro de la whitelist) y
se persiste.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from app.config import get_settings
from app.data.event_analyzer import resolve_scope_symbols
from app.db.session import get_engine, get_sessionmaker, session_scope
from app.logging_config import configure_logging
from app.repositories.events import EventoInput, save_events

logger = logging.getLogger(__name__)

_EVENTS_DIR = Path(__file__).resolve().parent.parent / "data" / "events"

_REQUIRED_FIELDS = ("external_id", "event_date", "event_type", "description")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Valida los archivos de catálogo (campos, fechas, scope) sin escribir en la base de datos.",
    )
    return parser.parse_args(argv)


def _load_catalog_file(path: Path) -> list[EventoInput]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: se esperaba un array JSON de eventos en el nivel superior.")

    events: list[EventoInput] = []
    for i, item in enumerate(raw):
        missing = [f for f in _REQUIRED_FIELDS if not item.get(f)]
        if missing:
            raise ValueError(f"{path}: evento #{i} le faltan campos obligatorios: {missing}.")

        try:
            event_date = date.fromisoformat(item["event_date"])
        except ValueError as exc:
            raise ValueError(
                f"{path}: evento {item['external_id']!r} tiene event_date inválida "
                f"({item['event_date']!r}, se espera YYYY-MM-DD)."
            ) from exc

        scope = item.get("scope")
        resolve_scope_symbols(scope)  # lanza ValueError si el scope tiene símbolos inválidos

        events.append(
            EventoInput(
                external_id=item["external_id"],
                event_date=event_date,
                event_type=item["event_type"],
                description=item["description"],
                severity=item.get("severity"),
                source=item.get("source"),
                scope=scope,
            )
        )
    return events


def load_catalog(events_dir: Path = _EVENTS_DIR) -> list[EventoInput]:
    """Carga y valida todos los `*.json` de `events_dir`. Falla explícito
    ante un `external_id` duplicado entre archivos (o dentro de uno): sería
    un upsert silenciosamente sobrescribiendo un evento con otro."""
    files = sorted(events_dir.glob("*.json"))
    all_events: list[EventoInput] = []
    seen_ids: dict[str, Path] = {}

    for path in files:
        for event in _load_catalog_file(path):
            if event.external_id in seen_ids:
                raise ValueError(
                    f"external_id duplicado {event.external_id!r} en {path} "
                    f"(ya definido en {seen_ids[event.external_id]})."
                )
            seen_ids[event.external_id] = path
            all_events.append(event)

    return all_events


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv)

    if not _EVENTS_DIR.exists() or not any(_EVENTS_DIR.glob("*.json")):
        print(
            f"No hay archivos de catálogo en {_EVENTS_DIR} todavía. "
            f"Ver {_EVENTS_DIR}/README.md para el formato."
        )
        return 0

    try:
        events = load_catalog()
    except ValueError as exc:
        print(f"FALLO validando el catálogo: {exc}")
        return 1

    print(f"Catálogo válido: {len(events)} eventos en {len(list(_EVENTS_DIR.glob('*.json')))} archivo(s).")

    if args.dry_run:
        print("(--dry-run: no se escribió nada en la base de datos)")
        return 0

    settings = get_settings()
    engine = get_engine(settings)
    session_factory = get_sessionmaker(engine)

    with session_scope(session_factory) as session:
        nuevos = save_events(session, events)

    print(f"OK: {nuevos} eventos nuevos, {len(events) - nuevos} actualizados (ya existían por external_id).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
