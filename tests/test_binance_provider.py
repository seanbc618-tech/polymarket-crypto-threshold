"""Binance public REST adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from crypto_threshold.adapters.prices.binance import BinanceProvider

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("asset", "symbol"),
    [
        ("BTC", "BTCUSDT"),
        ("ETH", "ETHUSDT"),
        ("SOL", "SOLUSDT"),
        ("XRP", "XRPUSDT"),
    ],
)
def test_ticker_uses_supported_usdt_symbol(asset: str, symbol: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == symbol
        return httpx.Response(200, json={"symbol": symbol, "price": "105000.50"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = BinanceProvider(client=client, clock=lambda: NOW).get_ticker_price(asset)
    assert snapshot.price == Decimal("105000.50")
    assert (snapshot.quote, snapshot.provider, snapshot.price_kind) == (
        "USDT",
        "binance",
        "last",
    )


def test_latest_snapshot_uses_only_closed_one_minute_candle() -> None:
    closed_ms = int((NOW.timestamp() - 60) * 1000)
    future_ms = int((NOW.timestamp() + 60) * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                [closed_ms - 59_000, "1", "2", "1", "100", "1", closed_ms],
                [closed_ms, "1", "2", "1", "999", "1", future_ms],
            ],
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = BinanceProvider(client=client, clock=lambda: NOW)
    series = provider.get_klines("BTC", interval="1m", limit=2)
    snapshot = provider.latest_close_snapshot(series, now=NOW)
    assert snapshot.price == Decimal("100")
    assert snapshot.price_kind == "1m_close"
    assert snapshot.observed_at < NOW


def test_unsupported_asset_raises_before_network() -> None:
    provider = BinanceProvider(client=httpx.Client(transport=httpx.MockTransport(lambda _: None)))
    try:
        provider.get_ticker_price("HYPE")
    except ValueError as exc:
        assert "HYPE" in str(exc)
    else:
        raise AssertionError("unsupported asset did not raise")


def test_ticker_symbol_mismatch_rejects() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"symbol": "ETHUSDT", "price": "100"})
    )
    provider = BinanceProvider(client=httpx.Client(transport=transport), clock=lambda: NOW)
    try:
        provider.get_ticker_price("BTC")
    except ValueError as exc:
        assert "symbol mismatch" in str(exc)
    else:
        raise AssertionError("mismatched symbol did not raise")
