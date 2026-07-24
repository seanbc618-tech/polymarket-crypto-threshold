"""Coinbase public spot sanity adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx

from crypto_threshold.adapters.prices.coinbase import CoinbaseProvider

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def test_coinbase_snapshot_is_usd_sanity_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/prices/BTC-USD/spot")
        return httpx.Response(
            200,
            json={"data": {"base": "BTC", "currency": "USD", "amount": "104999"}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    snapshot = CoinbaseProvider(client=client, clock=lambda: NOW).get_spot_price("BTC")
    assert snapshot.price == Decimal("104999")
    assert (snapshot.provider, snapshot.symbol, snapshot.quote) == (
        "coinbase",
        "BTC-USD",
        "USD",
    )


def test_unsupported_asset_raises_before_network() -> None:
    provider = CoinbaseProvider(
        client=httpx.Client(transport=httpx.MockTransport(lambda _: None))
    )
    try:
        provider.get_spot_price("SOL")
    except ValueError as exc:
        assert "SOL" in str(exc)
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
