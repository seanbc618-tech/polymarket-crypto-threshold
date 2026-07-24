"""Cross-provider identity, quote, freshness, and divergence gates."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from crypto_threshold.domain.prices import PriceSnapshot
from crypto_threshold.services.pricing_service import cross_check_prices
from tests.conftest import NOW


def _snapshot(
    price: str,
    *,
    provider: str,
    asset: str = "BTC",
    quote: str | None = None,
    age_seconds: int = 0,
) -> PriceSnapshot:
    resolved_quote = quote or ("USDT" if provider == "binance" else "USD")
    symbol = f"{asset}{resolved_quote}" if provider == "binance" else f"{asset}-{resolved_quote}"
    return PriceSnapshot(
        asset=asset,
        quote=resolved_quote,
        provider=provider,
        symbol=symbol,
        price=Decimal(price),
        price_kind="spot",
        observed_at=NOW - timedelta(seconds=age_seconds),
        received_at=NOW,
        source_version="test-v1",
        raw_payload={},
    )


def test_expected_binance_coinbase_pair_passes() -> None:
    check = cross_check_prices(
        _snapshot("100000", provider="binance"),
        _snapshot("100100", provider="coinbase"),
        now=NOW,
    )
    assert check.ok is True
    assert "quote_basis_usdt_usd" in check.reasons


def test_asset_mismatch_fails() -> None:
    check = cross_check_prices(
        _snapshot("100000", provider="binance"),
        _snapshot("100000", provider="coinbase", asset="ETH"),
        now=NOW,
    )
    assert check.ok is False
    assert any(reason.startswith("asset_mismatch") for reason in check.reasons)


def test_quote_mismatch_fails() -> None:
    check = cross_check_prices(
        _snapshot("100000", provider="binance", quote="USD"),
        _snapshot("100000", provider="coinbase", quote="USD"),
        now=NOW,
    )
    assert check.ok is False
    assert any(reason.startswith("quote_mismatch") for reason in check.reasons)


def test_stale_price_fails() -> None:
    check = cross_check_prices(
        _snapshot("100000", provider="binance", age_seconds=121),
        _snapshot("100000", provider="coinbase"),
        now=NOW,
        max_age_seconds=120,
    )
    assert check.ok is False
    assert any(reason.startswith("stale_primary") for reason in check.reasons)


def test_provider_role_and_divergence_fail() -> None:
    check = cross_check_prices(
        _snapshot("100000", provider="coinbase", quote="USDT"),
        _snapshot("110000", provider="binance", quote="USD"),
        now=NOW,
    )
    assert check.ok is False
    assert any(reason.startswith("provider_mismatch") for reason in check.reasons)
    assert any(reason.startswith("divergence_exceeds_threshold") for reason in check.reasons)
