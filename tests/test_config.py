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
    assert settings.BINANCE_REFERENCE_STREAM_ENABLED is False
    assert settings.BINANCE_STREAM_PROXY_URL is None
    assert settings.PAPER_MIN_NET_EV == Decimal("0.02")


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
