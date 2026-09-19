"""Tests unitarios del dispatch de `fetch-candles` en app.main.

Cubren el parseo de argumentos y el manejo de errores de validación (que no
requieren red, porque la whitelist y las temporalidades se chequean antes
de llamar a Binance). La descarga real se prueba en
tests/integration/test_exchange_client.py.
"""

from app.main import main, run_fetch_candles


def test_run_fetch_candles_rejects_symbol_outside_whitelist(capsys):
    exit_code = run_fetch_candles("PEPEUSDT", "1d", 10)

    assert exit_code == 1
    assert "FALLO" in capsys.readouterr().out


def test_main_without_args_prints_usage(capsys):
    exit_code = main([])

    assert exit_code == 2
    assert "Uso" in capsys.readouterr().out


def test_main_fetch_candles_with_wrong_arg_count_prints_usage(capsys):
    exit_code = main(["fetch-candles", "BTCUSDT"])

    assert exit_code == 2
    assert "Uso" in capsys.readouterr().out


def test_main_fetch_candles_with_non_integer_limit(capsys):
    exit_code = main(["fetch-candles", "BTCUSDT", "1d", "no-es-un-numero"])

    assert exit_code == 2
    assert "FALLO" in capsys.readouterr().out
