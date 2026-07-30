"""Tests for application settings."""

from __future__ import annotations

import os
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_threshold.config import Settings


def test_trading_disabled_defaults_true() -> None:
    s = Settings()
    assert s.TRADING_DISABLED is True


def test_database_path_default() -> None:
    s = Settings()
    assert s.DATABASE_PATH == "crypto_threshold.db"


def test_stream_defaults_to_disabled_read_only_shadow() -> None:
    settings = Settings(_env_file=None)
    assert settings.POLYMARKET_STREAM_ENABLED is False
    assert settings.POLYMARKET_STREAM_SHADOW_MODE is True
    assert settings.POLYMARKET_STREAM_USER_CHANNEL_ENABLED is False
    assert settings.POLYMARKET_STREAM_STALE_SECONDS == 45
    assert settings.POLYMARKET_STREAM_REST_VERIFY_SECONDS == 90
    assert settings.SHADOW_ENABLED is False
    assert settings.SHADOW_CONTRACT_FAMILY == "daily_threshold"
    assert settings.SHADOW_SETTLEMENT_LIMIT == 10
    assert settings.BINANCE_REFERENCE_STREAM_ENABLED is False
    assert settings.CHAINLINK_REFERENCE_STREAM_ENABLED is False
    assert settings.CHAINLINK_VOLATILITY_SAMPLE_SECONDS == 30
    assert settings.SHORT_CEX_MODEL_PATH == "data/models/cex-direction-v1.json"
    assert settings.SHORT_CEX_MIN_REMAINING_SECONDS == 10
    assert settings.SHORT_CEX_MAX_CHECKPOINT_LAG_SECONDS == 50
    assert settings.SHORT_CHALLENGER_ENABLED is False
    assert settings.short_challenger_checkpoints == (180, 120, 60, 30)
    assert settings.short_challenger_latencies_ms == (0, 100, 250, 500, 1000)
    assert settings.SHORT_CHALLENGER_MIN_REMAINING_SECONDS == 5
    assert settings.BINANCE_STREAM_PROXY_URL is None
    assert settings.PAPER_MIN_NET_EV == Decimal("0.02")
    assert settings.MICROSTRUCTURE_ENABLED is False
    assert settings.microstructure_symbols == (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
    )
    assert settings.MICROSTRUCTURE_DEPTH_LEVELS == 5
    assert settings.MICROSTRUCTURE_INTEGRITY_SAMPLE_LIMIT == 500


def test_binance_stream_proxy_is_explicit_no_auth_origin() -> None:
    settings = Settings(
        _env_file=None,
        BINANCE_STREAM_PROXY_URL="http://127.0.0.1:12334/",
    )
    assert settings.BINANCE_STREAM_PROXY_URL == "http://127.0.0.1:12334"
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            BINANCE_STREAM_PROXY_URL="http://user:secret@127.0.0.1:12334",
        )


def test_settings_from_env(monkeypatch: object) -> None:
    # Clear cached settings
    from crypto_threshold.config import get_settings
    get_settings.cache_clear()

    os.environ["TRADING_DISABLED"] = "false"
    try:
        s = Settings()
        assert s.TRADING_DISABLED is False
    finally:
        del os.environ["TRADING_DISABLED"]
        get_settings.cache_clear()


def test_challenger_grid_rejects_undeclared_or_unsorted_values() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SHORT_CHALLENGER_CHECKPOINTS_SECONDS="60,180,120,30",
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            SHORT_CHALLENGER_LATENCIES_MS="100,0,250",
        )


def test_microstructure_symbols_are_unique_supported_csv() -> None:
    settings = Settings(
        _env_file=None,
        MICROSTRUCTURE_SYMBOLS="btcUSDT, ETHUSDT",
    )
    assert settings.microstructure_symbols == ("BTCUSDT", "ETHUSDT")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MICROSTRUCTURE_SYMBOLS="BTCUSDT,BTCUSDT")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MICROSTRUCTURE_SYMBOLS="BTC-USD")
