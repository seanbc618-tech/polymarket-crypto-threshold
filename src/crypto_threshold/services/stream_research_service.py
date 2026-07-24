"""Serial, read-only coordinator for Polymarket stream hints."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from crypto_threshold.adapters.polymarket.stream import (
    CryptoLadderKey,
    DesiredSubscription,
    PolymarketStreamBridge,
    crypto_ladder_key,
    select_stream_tokens,
)
from crypto_threshold.storage.repositories import Repository


@dataclass(frozen=True)
class StreamPulseResult:
    """Hints produced by one main-thread drain; no exchange truth is changed."""

    status: str
    reprice_ladders: tuple[tuple[CryptoLadderKey, tuple[str, ...]], ...]
    reprice_market_ids: tuple[str, ...]
    reconcile_due: bool
    rest_fallback_active: bool
    health: Mapping[str, Any]


class StreamResearchCoordinator:
    """Own subscription selection and drain semantics, never analysis or SQL writes."""

    def __init__(
        self,
        *,
        repository: Repository,
        bridge: PolymarketStreamBridge | None,
        candidate_group_cap: int = 4,
    ) -> None:
        self.repository = repository
        self.bridge = bridge
        self.candidate_group_cap = max(0, candidate_group_cap)

    def start(self) -> bool:
        """Start the optional acceleration layer; failure leaves REST available."""
        if self.bridge is None:
            return False
        try:
            self.bridge.start()
        except Exception:
            return False
        return True

    def stop(self) -> None:
        if self.bridge is None:
            return
        try:
            self.bridge.stop()
        except Exception:
            pass

    def sync_subscriptions(
        self,
        *,
        positions: Sequence[Mapping[str, Any]] = (),
        open_orders: Sequence[Mapping[str, Any]] = (),
        active_market_ids: Sequence[str] = (),
    ) -> DesiredSubscription:
        rows = self._market_rows()
        ranked = [dict(row) for row in self.repository.list_ranked_stream_candidates(limit=200)]
        desired = select_stream_tokens(
            positions=positions,
            open_orders=open_orders,
            active_market_ids=active_market_ids,
            ranked_candidates=ranked,
            market_rows=rows,
            candidate_group_cap=self.candidate_group_cap,
        )
        if self.bridge is not None:
            self.bridge.set_desired_tokens(
                desired.token_ids,
                token_to_market=desired.token_to_market,
            )
        return desired

    def pulse(self) -> StreamPulseResult:
        """Drain changed BBOs and expand them to complete crypto ladders."""
        if self.bridge is None:
            return StreamPulseResult(
                status="disabled",
                reprice_ladders=(),
                reprice_market_ids=(),
                reconcile_due=False,
                rest_fallback_active=True,
                health={"status": "disabled", "detail": {"rest_fallback_active": True}},
            )
        try:
            batch = self.bridge.drain()
            health = self.bridge.health().public_dict()
        except Exception:
            return StreamPulseResult(
                status="degraded",
                reprice_ladders=(),
                reprice_market_ids=(),
                reconcile_due=False,
                rest_fallback_active=True,
                health={"status": "degraded", "detail": {"rest_fallback_active": True}},
            )

        rows = self._market_rows()
        token_map = self.bridge.token_to_market()
        group_members: dict[CryptoLadderKey, list[str]] = {}
        market_group: dict[str, CryptoLadderKey] = {}
        for market_id, row in rows.items():
            key = crypto_ladder_key(row)
            group_members.setdefault(key, []).append(market_id)
            market_group[market_id] = key

        changed_groups: list[CryptoLadderKey] = []
        for token_id in batch.quotes:
            mapped_market_id = token_map.get(token_id)
            quote_key = market_group.get(mapped_market_id or "")
            if quote_key is not None and quote_key not in changed_groups:
                changed_groups.append(quote_key)
        for hint in batch.tick_size:
            mapped_market_id = token_map.get(hint.token_id)
            tick_key = market_group.get(mapped_market_id or "")
            if tick_key is not None and tick_key not in changed_groups:
                changed_groups.append(tick_key)

        ladders = tuple(
            (key, tuple(sorted(group_members.get(key, ())))) for key in changed_groups
        )
        market_ids = tuple(
            dict.fromkeys(market_id for _, members in ladders for market_id in members)
        )
        raw_detail = health.get("detail")
        detail: Mapping[str, Any] = raw_detail if isinstance(raw_detail, Mapping) else {}
        fallback = bool(detail.get("rest_fallback_active", True))
        return StreamPulseResult(
            status=str(health.get("status") or "degraded"),
            reprice_ladders=ladders,
            reprice_market_ids=market_ids,
            reconcile_due=bool(batch.reconcile_due or batch.resolved),
            rest_fallback_active=fallback,
            health=health,
        )

    def mark_rest_verified(self, token_id: str) -> None:
        if self.bridge is not None:
            self.bridge.mark_rest_verified(token_id)

    def _market_rows(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["market_id"]): dict(row)
            for row in self.repository.list_stream_market_rows(limit=2000)
        }
