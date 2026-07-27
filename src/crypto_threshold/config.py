"""Application settings loaded from environment or ``.env``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration for the read-only research prototype."""

    DATABASE_PATH: str = "crypto_threshold.db"
    TRADING_DISABLED: bool = True

    # The private key is a secret input only. The dashboard stores it in macOS
    # Keychain and never writes it to SQLite or a normal config file.
    POLYMARKET_PRIVATE_KEY: str | None = None
    POLYMARKET_FUNDER: str | None = None
    POLYMARKET_GAMMA_API_BASE: str = "https://gamma-api.polymarket.com"
    POLYMARKET_CLOB_API_BASE: str = "https://clob.polymarket.com"
    POLYMARKET_SITE_API_BASE: str = "https://polymarket.com/api"
    BINANCE_API_BASE: str = "https://api.binance.com/api/v3"
    BINANCE_STREAM_URL: str = "wss://stream.binance.com:443"
    BINANCE_STREAM_PROXY_URL: str | None = None
    COINBASE_API_BASE: str = "https://api.coinbase.com/v2"

    PRICE_PRIMARY_PROVIDER: str = "binance"
    PRICE_SECONDARY_PROVIDER: str = "coinbase"
    PRICE_CROSSCHECK_MAX_DIFF: Decimal = Decimal("0.005")
    MAX_BOOK_AGE_SECONDS: int = 90
    MAX_PRICE_AGE_SECONDS: int = 120
    ANALYSIS_SIZE_USDC: Decimal = Decimal("10")
    HTTP_TIMEOUT_SECONDS: float = 20.0

    # Optional official-SDK market stream. It is acceleration-only and defaults
    # to disabled shadow mode; REST remains the analysis and execution authority.
    POLYMARKET_STREAM_ENABLED: bool = False
    POLYMARKET_STREAM_SHADOW_MODE: bool = True
    POLYMARKET_STREAM_USER_CHANNEL_ENABLED: bool = False
    POLYMARKET_STREAM_STALE_SECONDS: float = Field(default=45.0, gt=0)
    POLYMARKET_STREAM_REST_VERIFY_SECONDS: float = Field(default=90.0, gt=0)
    POLYMARKET_STREAM_CANDIDATE_GROUP_CAP: int = Field(default=4, ge=0)
    POLYMARKET_STREAM_MAX_QUOTE_SLOTS: int = Field(default=512, ge=1)

    # Phase 2 remains opt-in. These values control research/paper behavior only.
    SHADOW_ENABLED: bool = False
    SHADOW_CONTRACT_FAMILY: Literal[
        "daily_threshold", "short_updown"
    ] = "daily_threshold"
    SHADOW_INTERVAL_SECONDS: float = Field(default=60.0, gt=0)
    SHADOW_DISCOVERY_LIMIT: int = Field(default=20, ge=1, le=500)
    SHADOW_ANALYSIS_LIMIT: int = Field(default=10, ge=1, le=200)
    PAPER_MIN_NET_EV: Decimal = Field(default=Decimal("0.02"), ge=0)
    BINANCE_REFERENCE_STREAM_ENABLED: bool = False
    BINANCE_REFERENCE_STREAM_STALE_SECONDS: float = Field(default=45.0, gt=0)
    BINANCE_REFERENCE_STREAM_MAX_TICK_SLOTS: int = Field(default=16, ge=1)
    CHAINLINK_REFERENCE_STREAM_ENABLED: bool = False
    CHAINLINK_REFERENCE_STREAM_STALE_SECONDS: float = Field(default=5.0, gt=0)
    CHAINLINK_REFERENCE_STREAM_HISTORY_SECONDS: float = Field(default=1_200.0, ge=300)
    CHAINLINK_REFERENCE_STREAM_MAX_TICKS_PER_PAIR: int = Field(default=2_000, ge=60)
    CHAINLINK_VOLATILITY_WINDOW_SECONDS: int = Field(default=900, ge=60, le=3600)
    CHAINLINK_VOLATILITY_SAMPLE_SECONDS: int = Field(default=30, ge=1, le=60)
    CALIBRATION_BINS: int = Field(default=10, ge=2, le=100)
    CALIBRATION_MIN_TRAIN_SIZE: int = Field(default=30, ge=1)

    DASHBOARD_PUBLIC_ORIGIN: str | None = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER", mode="before")
    @classmethod
    def empty_wallet_value_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("BINANCE_STREAM_PROXY_URL", mode="before")
    @classmethod
    def binance_proxy_is_exact_http_origin(cls, value: object) -> str | None:
        if value is None or not str(value).strip():
            return None
        raw = str(value).strip()
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "BINANCE_STREAM_PROXY_URL must have a valid port"
            ) from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or port is None
        ):
            raise ValueError(
                "BINANCE_STREAM_PROXY_URL must be an exact unauthenticated "
                "HTTP(S) proxy origin such as http://127.0.0.1:12334"
            )
        return raw.rstrip("/")

    @field_validator("DASHBOARD_PUBLIC_ORIGIN", mode="before")
    @classmethod
    def public_origin_is_exact_https_origin(cls, value: object) -> str | None:
        if value is None or not str(value).strip():
            return None
        raw = str(value).strip()
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443}
        ):
            raise ValueError(
                "DASHBOARD_PUBLIC_ORIGIN must be an exact HTTPS origin "
                "such as https://crypto.example.com"
            )
        return f"https://{parsed.hostname.rstrip('.').lower()}"

    @property
    def wallet_configured(self) -> bool:
        return bool(self.POLYMARKET_PRIVATE_KEY and self.POLYMARKET_FUNDER)


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()


def load_settings(
    *,
    env_file: str | Path | None = None,
    secrets: Mapping[str, str] | None = None,
    reject_environment_secrets: bool = False,
) -> Settings:
    """Load settings with ephemeral secrets taking precedence over config files.

    This deliberately passes Keychain values directly to Pydantic instead of
    copying them into process environment variables.
    """

    values: dict[str, Any] = {}
    environment_private_key = str(os.environ.get("POLYMARKET_PRIVATE_KEY", "")).strip()
    if reject_environment_secrets and environment_private_key:
        raise ValueError(
            "POLYMARKET_PRIVATE_KEY must be removed from the process environment "
            "and stored in macOS Keychain"
        )
    private_key = (secrets or {}).get("POLYMARKET_PRIVATE_KEY")
    if private_key:
        values["POLYMARKET_PRIVATE_KEY"] = private_key
    if env_file is not None:
        values["_env_file"] = str(env_file)
    return Settings(**values)
