"""Coinbase public spot sanity adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from crypto_threshold.adapters.prices.coinbase import CoinbaseProvider

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("asset", "symbol"),
    [
        ("BTC", "BTC-USD"),
        ("ETH", "ETH-USD"),
        ("SOL", "SOL-USD"),
        ("XRP", "XRP-USD"),
    ],
)
def test_coinbase_snapshot_is_usd_sanity_only(asset: str, symbol: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/prices/{symbol}/spot")
        return httpx.Response(
            200,
            json={"data": {"base": asset, "currency": "USD", "amount": "104999"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = CoinbaseProvider(client=client, clock=lambda: NOW).get_spot_price(asset)
    assert snapshot.price == Decimal("104999")
    assert (snapshot.provider, snapshot.symbol, snapshot.quote) == (
        "coinbase",
        symbol,
        "USD",
    )


def test_unsupported_asset_raises_before_network() -> None:
    provider = CoinbaseProvider(
        client=httpx.Client(transport=httpx.MockTransport(lambda _: None))
    )
    try:
        provider.get_spot_price("DOGE")
    except ValueError as exc:
        assert "DOGE" in str(exc)
    else:
        raise AssertionError("unsupported asset did not raise")


def test_response_asset_mismatch_rejects() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"data": {"base": "ETH", "currency": "USD", "amount": "100"}},
        )
    )
    provider = CoinbaseProvider(client=httpx.Client(transport=transport), clock=lambda: NOW)
    try:
        provider.get_spot_price("BTC")
    except ValueError as exc:
        assert "asset mismatch" in str(exc)
    else:
        raise AssertionError("mismatched asset did not raise")
