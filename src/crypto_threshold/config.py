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
    BINANCE_FUTURES_API_BASE: str = "https://fapi.binance.com/fapi/v1"
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
    SHADOW_SETTLEMENT_LIMIT: int = Field(default=10, ge=1, le=500)
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
    SHORT_CEX_MODEL_PATH: str = "data/models/cex-direction-v1.json"
    SHORT_CEX_MIN_REMAINING_SECONDS: int = Field(default=10, ge=1, le=120)
    SHORT_CEX_MAX_CHECKPOINT_LAG_SECONDS: int = Field(
        default=50,
        ge=1,
        le=240,
    )
    SHORT_CHALLENGER_ENABLED: bool = False
    SHORT_CHALLENGER_CHECKPOINTS_SECONDS: str = "180,120,60,30"
    SHORT_CHALLENGER_LATENCIES_MS: str = "0,100,250,500,1000"
    SHORT_CHALLENGER_MIN_REMAINING_SECONDS: int = Field(default=5, ge=1, le=60)
    CALIBRATION_BINS: int = Field(default=10, ge=2, le=100)
    CALIBRATION_MIN_TRAIN_SIZE: int = Field(default=30, ge=1)

    # Independent public CEX microstructure research store. This is never
    # shared with the Daily or short-Up/Down evidence databases.
    MICROSTRUCTURE_ENABLED: bool = False
    MICROSTRUCTURE_DATABASE_PATH: str = "data/microstructure-shadow.db"
    MICROSTRUCTURE_SYMBOLS: str = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT"
    MICROSTRUCTURE_POLL_SECONDS: float = Field(default=0.25, gt=0)
    MICROSTRUCTURE_SNAPSHOT_SECONDS: float = Field(default=60.0, gt=0)
    MICROSTRUCTURE_FEATURE_SECONDS: float = Field(default=5.0, gt=0)
    MICROSTRUCTURE_INTEGRITY_SECONDS: float = Field(default=300.0, gt=0)
    MICROSTRUCTURE_PURGE_SECONDS: float = Field(default=600.0, ge=0)
    MICROSTRUCTURE_EMBARGO_SECONDS: float = Field(default=300.0, ge=0)
    MICROSTRUCTURE_DEPTH_LEVELS: int = Field(default=5, ge=1, le=100)
    MICROSTRUCTURE_TRADE_LOOKBACK_SECONDS: float = Field(default=5.0, gt=0)
    MICROSTRUCTURE_EVENT_BATCH_LIMIT: int = Field(default=50_000, ge=1, le=200_000)
    MICROSTRUCTURE_INTEGRITY_SAMPLE_LIMIT: int = Field(
        default=500,
        ge=102,
        le=10_000,
    )
    MICROSTRUCTURE_STREAM_STALE_SECONDS: float = Field(default=30.0, gt=0)
    MICROSTRUCTURE_STREAM_READY_TIMEOUT_SECONDS: float = Field(default=30.0, gt=0)
    MICROSTRUCTURE_STREAM_MAX_EVENTS: int = Field(
        default=200_000,
        ge=1_000,
        le=2_000_000,
    )
    MICROSTRUCTURE_FROZEN_MODEL_VERSION: str = (
        "cex-kline-chainlink-direction-v1+49093373ec3e"
    )
    MICROSTRUCTURE_DURATION_HOURS: float = Field(default=2.0, gt=0)

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

    @field_validator("SHORT_CHALLENGER_CHECKPOINTS_SECONDS")
    @classmethod
    def challenger_checkpoints_are_declared_csv(cls, value: object) -> str:
        checkpoints = _csv_integers(value, field="SHORT_CHALLENGER_CHECKPOINTS_SECONDS")
        if (
            len(checkpoints) != 4
            or len(set(checkpoints)) != len(checkpoints)
            or any(checkpoint < 30 or checkpoint > 300 for checkpoint in checkpoints)
            or tuple(sorted(checkpoints, reverse=True)) != checkpoints
        ):
            raise ValueError(
                "SHORT_CHALLENGER_CHECKPOINTS_SECONDS must be four unique "
                "descending values within [30, 300]"
            )
        return ",".join(str(checkpoint) for checkpoint in checkpoints)

    @field_validator("SHORT_CHALLENGER_LATENCIES_MS")
    @classmethod
    def challenger_latencies_are_declared_csv(cls, value: object) -> str:
        latencies = _csv_integers(value, field="SHORT_CHALLENGER_LATENCIES_MS")
        if (
            not latencies
            or latencies[0] != 0
            or len(set(latencies)) != len(latencies)
            or any(latency < 0 or latency > 5_000 for latency in latencies)
            or tuple(sorted(latencies)) != latencies
        ):
            raise ValueError(
                "SHORT_CHALLENGER_LATENCIES_MS must start at zero and contain "
                "unique ascending values within [0, 5000]"
            )
        return ",".join(str(latency) for latency in latencies)

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

    @field_validator("MICROSTRUCTURE_SYMBOLS")
    @classmethod
    def microstructure_symbols_are_declared_csv(cls, value: object) -> str:
        symbols = _csv_symbols(value, field="MICROSTRUCTURE_SYMBOLS")
        return ",".join(symbols)

    @field_validator("MICROSTRUCTURE_FROZEN_MODEL_VERSION")
    @classmethod
    def microstructure_frozen_model_version_is_exact(cls, value: object) -> str:
        raw = str(value).strip()
        model_name, separator, digest = raw.rpartition("+")
        if (
            separator != "+"
            or not model_name
            or len(digest) != 12
            or digest != digest.lower()
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(
                "MICROSTRUCTURE_FROZEN_MODEL_VERSION must end with an exact "
                "12-character lowercase artifact-hash prefix"
            )
        return raw

    @property
    def wallet_configured(self) -> bool:
        return bool(self.POLYMARKET_PRIVATE_KEY and self.POLYMARKET_FUNDER)

    @property
    def short_challenger_checkpoints(self) -> tuple[int, ...]:
        return _csv_integers(
            self.SHORT_CHALLENGER_CHECKPOINTS_SECONDS,
            field="SHORT_CHALLENGER_CHECKPOINTS_SECONDS",
        )

    @property
    def short_challenger_latencies_ms(self) -> tuple[int, ...]:
        return _csv_integers(
            self.SHORT_CHALLENGER_LATENCIES_MS,
            field="SHORT_CHALLENGER_LATENCIES_MS",
        )

    @property
    def microstructure_symbols(self) -> tuple[str, ...]:
        return _csv_symbols(
            self.MICROSTRUCTURE_SYMBOLS,
            field="MICROSTRUCTURE_SYMBOLS",
        )


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


def _csv_integers(value: object, *, field: str) -> tuple[int, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a comma-separated string")
    try:
        parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise ValueError(f"{field} contains a non-integer value") from exc
    if not parsed:
        raise ValueError(f"{field} must not be empty")
    return parsed


def _csv_symbols(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a comma-separated string")
    symbols = tuple(
        item.strip().upper()
        for item in value.split(",")
        if item.strip()
    )
    if (
        not symbols
        or len(set(symbols)) != len(symbols)
        or any(
            not symbol.endswith("USDT")
            or not symbol[:-4].isalnum()
            or len(symbol) > 20
            for symbol in symbols
        )
    ):
        raise ValueError(f"{field} contains an invalid or duplicate symbol")
    return symbols
