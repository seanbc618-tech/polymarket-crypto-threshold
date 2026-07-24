"""Single owner for Gamma discovery and candidate persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

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
        results: list[DiscoveryResult] = []
        for payload in payloads:
            received_at = _utc(self.clock())
            market = translate_market(payload, received_at=received_at)
            if not market.market_id:
                continue
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
