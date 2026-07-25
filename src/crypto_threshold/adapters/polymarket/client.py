"""Public Gamma and CLOB GET client. No trading methods exist here."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from crypto_threshold.adapters.polymarket.base import MarketEventContext
from crypto_threshold.config import Settings
from crypto_threshold.domain.assets import DAILY_THRESHOLD_ASSETS, asset_contract


class GammaClobReadClient:
    """Read-only adapter for market discovery, books, and fee metadata."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.gamma_base = settings.POLYMARKET_GAMMA_API_BASE.rstrip("/")
        self.clob_base = settings.POLYMARKET_CLOB_API_BASE.rstrip("/")
        self._client = client or httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def discover_markets(self, asset: str | None, limit: int) -> list[dict[str, Any]]:
        assets = [asset.upper()] if asset else sorted(DAILY_THRESHOLD_ASSETS)
        queries: list[str] = []
        for symbol in assets:
            try:
                name = asset_contract(symbol).display_name
            except ValueError as exc:
                raise ValueError(f"unsupported discovery asset: {symbol}") from exc
            queries.extend((f"{name} above", f"{name} below"))
        per_query = max(1, (limit + len(queries) - 1) // len(queries))
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for query in queries:
            response = self._client.get(
                f"{self.gamma_base}/public-search",
                params={
                    "q": query,
                    "events_status": "active",
                    "keep_closed_markets": 0,
                    "limit_per_type": per_query,
                    "search_profiles": False,
                    "search_tags": False,
                },
            )
            response.raise_for_status()
            added = 0
            for item in _search_market_payloads(response.json()):
                if item.get("active") is False or item.get("closed") is True:
                    continue
                key = str(item.get("id") or item.get("conditionId") or item.get("slug") or "")
                if key and key not in seen:
                    seen.add(key)
                    results.append(item)
                    added += 1
                if added >= per_query:
                    break
        return results[:limit]

    def discover_updown_markets(
        self,
        intervals: tuple[str, ...],
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        tags = {"5m": "5M", "15m": "15M"}
        requested = tuple(dict.fromkeys(interval.lower() for interval in intervals))
        if not requested or any(interval not in tags for interval in requested):
            raise ValueError("up/down intervals must be 5m and/or 15m")
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        per_interval = max(7, (limit + len(requested) - 1) // len(requested))
        for interval in requested:
            response = self._client.get(
                f"{self.gamma_base}/events",
                params={
                    "tag_slug": tags[interval],
                    "closed": "false",
                    "end_date_min": start.isoformat(),
                    "end_date_max": end.isoformat(),
                    "limit": min(500, per_interval * 4),
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("unexpected Gamma up/down discovery response")
            added = 0
            for event in payload:
                if not isinstance(event, dict):
                    continue
                series_slug = str(event.get("seriesSlug") or "")
                if not series_slug:
                    series = event.get("series") or []
                    if series and isinstance(series[0], dict):
                        series_slug = str(series[0].get("slug") or "")
                if not series_slug.lower().endswith(f"-{interval}"):
                    continue
                for market in event.get("markets") or []:
                    if not isinstance(market, dict):
                        continue
                    key = str(
                        market.get("id")
                        or market.get("conditionId")
                        or market.get("slug")
                        or ""
                    )
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    results.append({**market, "events": [event]})
                    added += 1
                    if added >= per_interval or len(results) >= limit:
                        break
                if added >= per_interval or len(results) >= limit:
                    break
        return results[:limit]

    def get_market(self, market_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self.gamma_base}/markets/{market_id}")
        if response.status_code == 404:
            response = self._client.get(
                f"{self.gamma_base}/markets", params={"condition_ids": market_id}
            )
            response.raise_for_status()
            payload = response.json()
            items = payload if isinstance(payload, list) else payload.get("markets", [])
            if not items:
                raise LookupError(f"Gamma market not found: {market_id}")
            item = items[0]
            if not isinstance(item, dict):
                raise ValueError("unexpected Gamma market response")
            return item
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected Gamma market response")
        return payload

    def get_event(self, event_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self.gamma_base}/events/{event_id}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected Gamma event response")
        return payload

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self.clob_base}/book", params={"token_id": token_id})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected CLOB book response")
        return payload

    def get_market_event_context(
        self, market_id: str, condition_id: str | None, question: str
    ) -> MarketEventContext:
        response = self._client.get(
            f"{self.gamma_base}/public-search",
            params={
                "q": question,
                "events_status": "active",
                "keep_closed_markets": 0,
                "limit_per_type": 10,
                "search_profiles": False,
                "search_tags": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected Gamma event-context response")
        for event in payload.get("events") or []:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets") or []:
                if not isinstance(market, dict):
                    continue
                identifiers = {
                    str(market.get("id") or ""),
                    str(market.get("conditionId") or ""),
                }
                if market_id in identifiers or (condition_id and condition_id in identifiers):
                    return MarketEventContext(
                        event_id=str(event.get("id")) if event.get("id") is not None else None,
                        raw_payload=payload,
                    )
        return MarketEventContext(event_id=None, raw_payload=payload)

    def get_market_info(self, condition_id: str) -> dict[str, Any]:
        response = self._client.get(f"{self.clob_base}/clob-markets/{condition_id}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("unexpected CLOB market-info response")
        return payload

    def get_server_time(self) -> int | float | str:
        response = self._client.get(f"{self.clob_base}/time")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, (int, float, str)):
            raise ValueError("unexpected CLOB server-time response")
        return payload


def _search_market_payloads(payload: Any) -> list[dict[str, Any]]:
    """Flatten public-search event envelopes while preserving event identity."""
    found: list[dict[str, Any]] = []
    if isinstance(payload, list):
        for item in payload:
            found.extend(_search_market_payloads(item))
        return found
    if not isinstance(payload, dict):
        return found
    if payload.get("question") and (payload.get("id") or payload.get("conditionId")):
        return [payload]
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            markets = event.get("markets")
            if not isinstance(markets, list):
                continue
            for market in markets:
                if isinstance(market, dict):
                    found.append({**market, "events": [event]})
    markets = payload.get("markets")
    if isinstance(markets, list):
        found.extend(item for item in markets if isinstance(item, dict))
    return found
