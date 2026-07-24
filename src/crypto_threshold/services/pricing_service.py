"""Cross-provider price sanity checks with identity and freshness gates."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from crypto_threshold.domain.prices import PriceCrossCheck, PriceSnapshot

DEFAULT_DIFF_THRESHOLD = Decimal("0.005")
PRICE_CROSS_CHECK_VERSION = "price-cross-check-v2"


def cross_check_prices(
    primary: PriceSnapshot,
    secondary: PriceSnapshot,
    *,
    max_diff: Decimal = DEFAULT_DIFF_THRESHOLD,
    max_age_seconds: int = 120,
    now: datetime | None = None,
) -> PriceCrossCheck:
    """Compare source identity, quote role, freshness, and price divergence."""
    now = _utc(now or datetime.now(UTC))
    reasons: list[str] = []
    ok = True

    if primary.provider != "binance" or secondary.provider != "coinbase":
        ok = False
        reasons.append(f"provider_mismatch:{primary.provider}/{secondary.provider}")
    if primary.asset != secondary.asset:
        ok = False
        reasons.append(f"asset_mismatch:{primary.asset}!={secondary.asset}")
    expected_quotes = (primary.quote, secondary.quote) == ("USDT", "USD")
    if not expected_quotes:
        ok = False
        reasons.append(f"quote_mismatch:{primary.quote}/{secondary.quote}")
    else:
        reasons.append("quote_basis_usdt_usd")

    for role, snapshot in (("primary", primary), ("secondary", secondary)):
        age = (now - _utc(snapshot.observed_at)).total_seconds()
        if age < -5:
            ok = False
            reasons.append(f"{role}_timestamp_in_future")
        elif age > max_age_seconds:
            ok = False
            reasons.append(f"stale_{role}:{int(age)}s")

    if primary.price <= 0 or secondary.price <= 0:
        ok = False
        relative_diff = Decimal("1")
        reasons.append("non_positive_price")
    else:
        relative_diff = abs(primary.price - secondary.price) / min(
            primary.price, secondary.price
        )
        if relative_diff > max_diff:
            ok = False
            reasons.append(f"divergence_exceeds_threshold:{relative_diff}")

    observed_at = min(_utc(primary.observed_at), _utc(secondary.observed_at))
    return PriceCrossCheck(
        asset=primary.asset,
        primary_provider=primary.provider,
        secondary_provider=secondary.provider,
        primary_price=primary.price,
        secondary_price=secondary.price,
        relative_diff=relative_diff,
        ok=ok,
        observed_at=observed_at,
        received_at=now,
        source_version=PRICE_CROSS_CHECK_VERSION,
        reasons=reasons,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
