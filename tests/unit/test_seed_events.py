"""Tests de scripts.seed_events: carga y validación del catálogo desde
archivos JSON, sin tocar la base de datos (eso lo cubre
tests/integration/test_events_repository.py)."""

import json

import pytest

from scripts.seed_events import load_catalog


def _write(tmp_path, name: str, events: list[dict]) -> None:
    (tmp_path / name).write_text(json.dumps(events), encoding="utf-8")


def test_loads_a_valid_catalog(tmp_path):
    _write(
        tmp_path,
        "catalogo.json",
        [
            {
                "external_id": "btc-halving-2024",
                "event_date": "2024-04-20",
                "event_type": "HALVING",
                "description": "Cuarto halving de Bitcoin.",
                "severity": "alta",
                "source": "https://example.com/halving",
                "scope": "BTCUSDT",
            }
        ],
    )

    events = load_catalog(tmp_path)

    assert len(events) == 1
    assert events[0].external_id == "btc-halving-2024"
    assert events[0].event_date.isoformat() == "2024-04-20"


def test_loads_across_multiple_files(tmp_path):
    _write(
        tmp_path,
        "a.json",
        [{"external_id": "e1", "event_date": "2024-01-01", "event_type": "OTRO", "description": "x"}],
    )
    _write(
        tmp_path,
        "b.json",
        [{"external_id": "e2", "event_date": "2024-02-01", "event_type": "OTRO", "description": "y"}],
    )

    events = load_catalog(tmp_path)

    assert {e.external_id for e in events} == {"e1", "e2"}


def test_missing_required_field_raises(tmp_path):
    _write(tmp_path, "catalogo.json", [{"external_id": "e1", "event_date": "2024-01-01", "event_type": "OTRO"}])

    with pytest.raises(ValueError, match="faltan campos obligatorios"):
        load_catalog(tmp_path)


def test_invalid_date_raises(tmp_path):
    _write(
        tmp_path,
        "catalogo.json",
        [{"external_id": "e1", "event_date": "20-enero-2024", "event_type": "OTRO", "description": "x"}],
    )

    with pytest.raises(ValueError, match="event_date inválida"):
        load_catalog(tmp_path)


def test_invalid_scope_symbol_raises(tmp_path):
    _write(
        tmp_path,
        "catalogo.json",
        [
            {
                "external_id": "e1",
                "event_date": "2024-01-01",
                "event_type": "OTRO",
                "description": "x",
                "scope": "FAKECOIN",
            }
        ],
    )

    with pytest.raises(ValueError, match="no autorizados"):
        load_catalog(tmp_path)


def test_duplicate_external_id_across_files_raises(tmp_path):
    _write(
        tmp_path,
        "a.json",
        [{"external_id": "e1", "event_date": "2024-01-01", "event_type": "OTRO", "description": "x"}],
    )
    _write(
        tmp_path,
        "b.json",
        [{"external_id": "e1", "event_date": "2024-02-01", "event_type": "OTRO", "description": "y"}],
    )

    with pytest.raises(ValueError, match="duplicado"):
        load_catalog(tmp_path)


def test_top_level_must_be_a_json_array(tmp_path):
    (tmp_path / "catalogo.json").write_text(json.dumps({"not": "a list"}), encoding="utf-8")

    with pytest.raises(ValueError, match="array JSON"):
        load_catalog(tmp_path)


def test_no_json_files_returns_empty_list(tmp_path):
    assert load_catalog(tmp_path) == []
