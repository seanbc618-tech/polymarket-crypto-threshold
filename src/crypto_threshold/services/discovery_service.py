"""Single owner for Gamma discovery and candidate persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from crypto_threshold.adapters.polymarket.base import PolymarketReadClient
from crypto_threshold.adapters.polymarket.translator import (
    GAMMA_SOURCE_VERSION,
    translate_market,
)
from crypto_threshold.domain.markets import CryptoMarket
from crypto_threshold.domain.rules import CryptoResolutionRule, parse_contract
from crypto_threshold.storage.repositories import Repository


@dataclass(frozen=True)
class DiscoveryResult:
    market: CryptoMarket
    rule: CryptoResolutionRule


class DiscoveryService:
    """Discover through Gamma, then persist through the canonical Repository."""

    def __init__(
        self,
        client: PolymarketReadClient,
        repository: Repository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def discover(self, *, asset: str | None = None, limit: int = 100) -> list[DiscoveryResult]:
        payloads = self.client.discover_markets(asset, limit)
        return self._persist(payloads)

    def discover_updown(
        self,
        *,
        intervals: tuple[str, ...] = ("5m", "15m"),
        limit: int = 50,
    ) -> list[DiscoveryResult]:
        """Persist only the currently open seven-asset markets for each interval."""
        now = _utc(self.clock())
        payloads = self.client.discover_updown_markets(
            intervals,
            start=now - timedelta(minutes=16),
            end=now + timedelta(minutes=16),
            limit=limit,
        )
        active: list[dict[str, object]] = []
        for payload in payloads:
            market = translate_market(payload, received_at=now)
            if (
                market.event_start_time is not None
                and market.gamma_end_date is not None
                and _utc(market.event_start_time) <= now < _utc(market.gamma_end_date)
            ):
                active.append(payload)
        return self._persist(active)

    def _persist(self, payloads: list[dict[str, object]]) -> list[DiscoveryResult]:
        results: list[DiscoveryResult] = []
        seen_market_ids: set[str] = set()
        for payload in payloads:
            received_at = _utc(self.clock())
            market = translate_market(payload, received_at=received_at)
            if not market.market_id or market.market_id in seen_market_ids:
                continue
            seen_market_ids.add(market.market_id)
            self.repository.upsert_market(market)
            self.repository.record_external_payload(
                market_id=market.market_id,
                source="gamma",
                payload_kind="market",
                payload=payload,
                observed_at=received_at,
                received_at=received_at,
                source_version=GAMMA_SOURCE_VERSION,
            )
            rule = parse_contract(market, now=received_at)
            self.repository.save_resolution_rule(
                market.market_id,
                rule,
                observed_at=received_at,
                received_at=received_at,
            )
            results.append(DiscoveryResult(market=market, rule=rule))
        return results


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
