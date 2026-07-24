"""Shared deterministic payloads for read-only workflow tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from crypto_threshold.adapters.polymarket.base import MarketEventContext
from crypto_threshold.domain.prices import Kline, KlineSeries, PriceSnapshot

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
TARGET = datetime(2026, 7, 23, 16, 0, tzinfo=UTC)


@pytest.fixture
def market_payload() -> dict[str, Any]:
    return make_market_payload()


def make_market_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "market-1",
        "eventId": "event-1",
        "conditionId": "condition-1",
        "question": "Will Bitcoin be above $100,000 on July 23, 2026?",
        "slug": "bitcoin-above-100000-july-23-2026",
        "description": (
            "This market resolves Yes using the Binance BTC/USDT 1-minute "
            "candle Close price at 12:00 PM ET."
        ),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "endDate": TARGET.isoformat().replace("+00:00", "Z"),
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
    }
    payload.update(overrides)
    return payload


def make_book(
    *,
    outcome: str,
    observed_at: datetime = NOW,
) -> dict[str, Any]:
    if outcome == "YES":
        bids = [{"price": "0.39", "size": "100"}]
        asks = [
            {"price": "0.40", "size": "10"},
            {"price": "0.42", "size": "100"},
        ]
    else:
        bids = [{"price": "0.58", "size": "100"}]
        asks = [
            {"price": "0.60", "size": "10"},
            {"price": "0.62", "size": "100"},
        ]
    return {
        "timestamp": str(int(observed_at.timestamp() * 1000)),
        "bids": bids,
        "asks": asks,
    }


class FakePolymarketClient:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        book_time: datetime = NOW,
        fee_payload: dict[str, Any] | None = None,
    ) -> None:
        self.payload = payload
        self.book_time = book_time
        self.fee_payload = fee_payload or {"fd": {"r": 0.07, "e": 1, "to": True}}
        self.reads: list[str] = []

    def discover_markets(self, asset: str | None, limit: int) -> list[dict[str, Any]]:
        self.reads.append("discover")
        return [self.payload][:limit]

    def get_market(self, market_id: str) -> dict[str, Any]:
        self.reads.append("market")
        return self.payload

    def get_market_event_context(
        self, market_id: str, condition_id: str | None, question: str
    ) -> MarketEventContext:
        self.reads.append("event_context")
        return MarketEventContext(event_id="event-1", raw_payload={"events": []})

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        self.reads.append(f"book:{token_id}")
        outcome = "YES" if token_id == "yes-token" else "NO"
        return make_book(outcome=outcome, observed_at=self.book_time)

    def get_market_info(self, condition_id: str) -> dict[str, Any]:
        self.reads.append("market_info")
        return self.fee_payload


class FakeBinanceProvider:
    def get_klines(self, asset: str, interval: str, limit: int) -> KlineSeries:
        if interval == "1m":
            klines = (
                _kline(NOW - timedelta(minutes=2), Decimal("104900")),
                _kline(NOW - timedelta(minutes=1), Decimal("105000")),
            )
        else:
            klines = tuple(
                _kline(NOW - timedelta(days=31 - index), Decimal(99000 + index * 211))
                for index in range(31)
            )
        return KlineSeries(
            asset=asset,
            quote="USDT",
            provider="binance",
            symbol=f"{asset}USDT",
            interval=interval,
            klines=klines,
            received_at=NOW,
            source_version="binance-test-v1",
            raw_payload=[
                [
                    int(kline.open_time.timestamp() * 1000),
                    str(kline.open),
                    str(kline.high),
                    str(kline.low),
                    str(kline.close),
                    str(kline.volume),
                    int(kline.close_time.timestamp() * 1000),
                ]
                for kline in klines
            ],
        )

    def latest_close_snapshot(
        self, series: KlineSeries, *, now: datetime | None = None
    ) -> PriceSnapshot:
        cutoff = now or NOW
        latest = max(
            (kline for kline in series.klines if kline.close_time <= cutoff),
            key=lambda kline: kline.close_time,
        )
        return PriceSnapshot(
            asset=series.asset,
            quote=series.quote,
            provider=series.provider,
            symbol=series.symbol,
            price=latest.close,
            price_kind="1m_close",
            observed_at=latest.close_time,
            received_at=series.received_at,
            source_version=series.source_version,
            raw_payload=series.raw_payload,
        )


class FakeCoinbaseProvider:
    def __init__(
        self,
        *,
        asset: str = "BTC",
        quote: str = "USD",
        observed_at: datetime = NOW,
    ) -> None:
        self.asset = asset
        self.quote = quote
        self.observed_at = observed_at

    def get_spot_price(self, asset: str) -> PriceSnapshot:
        return PriceSnapshot(
            asset=self.asset,
            quote=self.quote,
            provider="coinbase",
            symbol=f"{self.asset}-{self.quote}",
            price=Decimal("104900"),
            price_kind="spot",
            observed_at=self.observed_at,
            received_at=NOW,
            source_version="coinbase-test-v1",
            raw_payload={
                "data": {
                    "base": self.asset,
                    "currency": self.quote,
                    "amount": "104900",
                }
            },
        )


def _kline(close_time: datetime, close: Decimal) -> Kline:
    return Kline(
        open_time=close_time - timedelta(minutes=1),
        close_time=close_time,
        open=close - 1,
        high=close + 2,
        low=close - 2,
        close=close,
        volume=Decimal("100"),
    )
