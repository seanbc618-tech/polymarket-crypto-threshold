"""Polymarket's public crypto-window price contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

POLYMARKET_CRYPTO_PRICE_SOURCE_VERSION = "polymarket-crypto-price-v1"
POLYMARKET_CRYPTO_PRICE_SOURCE = "polymarket_site"
AUTHORITATIVE_WINDOW_PRICE_KIND = "authoritative_window_price"


@dataclass(frozen=True)
class CryptoWindowPrice:
    asset: str
    pair: str
    interval: str
    window_start_time_utc: datetime
    target_time_utc: datetime
    open_price: Decimal
    close_price: Decimal | None
    completed: bool
    incomplete: bool
    cached: bool
    provider_timestamp: datetime
    received_at: datetime
    source_version: str
    raw_payload: dict[str, Any]


def interval_variant(interval: str) -> str:
    variants = {"5m": "fiveminute", "15m": "fifteen"}
    try:
        return variants[interval]
    except KeyError as exc:
        raise ValueError(f"unsupported crypto-price interval: {interval}") from exc


def crypto_window_price_evidence(
    payload: dict[str, Any],
    *,
    asset: str,
    pair: str,
    interval: str,
    start: datetime,
    end: datetime,
    received_at: datetime,
) -> dict[str, Any]:
    return {
        "request": {
            "symbol": asset,
            "eventStartTime": _iso_z(start),
            "variant": interval_variant(interval),
            "endDate": _iso_z(end),
        },
        "normalized": {
            "provider": POLYMARKET_CRYPTO_PRICE_SOURCE,
            "settlement_provider": "chainlink",
            "pair": pair,
            "candle_interval": interval,
            "price_fields": ["openPrice", "closePrice"],
            "timezone": "UTC",
            "observation_time": "window_start",
            "provider_timestamp": payload.get("timestamp"),
            "received_at": _iso_z(received_at),
            "source_version": POLYMARKET_CRYPTO_PRICE_SOURCE_VERSION,
        },
        "response": payload,
    }


def parse_crypto_window_price(
    payload: dict[str, Any],
    *,
    asset: str,
    pair: str,
    interval: str,
    start: datetime,
    end: datetime,
    received_at: datetime,
) -> CryptoWindowPrice:
    start = _utc(start)
    end = _utc(end)
    received_at = _utc(received_at)
    if end <= start:
        raise ValueError("authoritative_window_time_not_increasing")
    interval_variant(interval)

    open_price = _positive_decimal(payload.get("openPrice"))
    if open_price is None:
        raise ValueError("missing_authoritative_window_open_price")
    provider_timestamp = _provider_timestamp(payload.get("timestamp"))
    if provider_timestamp is None:
        raise ValueError("missing_authoritative_window_provider_timestamp")
    if provider_timestamp > received_at + timedelta(seconds=5):
        raise ValueError("authoritative_window_provider_timestamp_in_future")

    close_price = _positive_decimal(payload.get("closePrice"))
    return CryptoWindowPrice(
        asset=asset,
        pair=pair,
        interval=interval,
        window_start_time_utc=start,
        target_time_utc=end,
        open_price=open_price,
        close_price=close_price,
        completed=payload.get("completed") is True,
        incomplete=payload.get("incomplete") is True,
        cached=payload.get("cached") is True,
        provider_timestamp=provider_timestamp,
        received_at=received_at,
        source_version=POLYMARKET_CRYPTO_PRICE_SOURCE_VERSION,
        raw_payload=crypto_window_price_evidence(
            payload,
            asset=asset,
            pair=pair,
            interval=interval,
            start=start,
            end=end,
            received_at=received_at,
        ),
    )


def _provider_timestamp(value: Any) -> datetime | None:
    try:
        timestamp = Decimal(str(value))
        if timestamp <= 0:
            return None
        if timestamp > Decimal("10000000000"):
            timestamp /= Decimal("1000")
        return datetime.fromtimestamp(float(timestamp), tz=UTC)
    except (InvalidOperation, ValueError, OSError, OverflowError):
        return None


def _positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 and parsed.is_finite() else None


def _iso_z(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
