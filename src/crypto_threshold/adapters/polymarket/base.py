"""Read-only Polymarket client contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MarketEventContext:
    event_id: str | None
    raw_payload: dict[str, Any]


class PolymarketReadClient(Protocol):
    """Only public GET operations are allowed in Phase 1."""

    def discover_markets(self, asset: str | None, limit: int) -> list[dict[str, Any]]: ...

    def get_market(self, market_id: str) -> dict[str, Any]: ...

    def get_market_event_context(
        self, market_id: str, condition_id: str | None, question: str
    ) -> MarketEventContext: ...

    def get_order_book(self, token_id: str) -> dict[str, Any]: ...

    def get_market_info(self, condition_id: str) -> dict[str, Any]: ...
