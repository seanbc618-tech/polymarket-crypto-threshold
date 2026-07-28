"""Public Gamma and CLOB GET client. No trading methods exist here."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from crypto_threshold.adapters.polymarket.base import MarketEventContext
from crypto_threshold.adapters.prices.polymarket_crypto import interval_variant
from crypto_threshold.config import Settings
from crypto_threshold.domain.assets import DAILY_THRESHOLD_ASSETS, asset_contract

NEW_YORK = ZoneInfo("America/New_York")
DAILY_SETTLEMENT_TIME = time(12, 0)


class GammaClobReadClient:
    """Read-only adapter for market discovery, books, and fee metadata."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.gamma_base = settings.POLYMARKET_GAMMA_API_BASE.rstrip("/")
        self.clob_base = settings.POLYMARKET_CLOB_API_BASE.rstrip("/")
        self.site_api_base = settings.POLYMARKET_SITE_API_BASE.rstrip("/")
        self._client = client or httpx.Client(timeout=settings.HTTP_TIMEOUT_SECONDS)
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def discover_markets(self, asset: str | None, limit: int) -> list[dict[str, Any]]:
        assets = [asset.upper()] if asset else sorted(DAILY_THRESHOLD_ASSETS)
        target_date = _next_daily_target_date(self._clock())
        date_label = f"{target_date.strftime('%B')} {target_date.day} {target_date.year}"
        per_asset = max(1, (limit + len(assets) - 1) // len(assets))
        query_limit = max(10, per_asset * 2)
        buckets: list[list[dict[str, Any]]] = []
        for symbol in assets:
            try:
                name = asset_contract(symbol).display_name
            except ValueError as exc:
                raise ValueError(f"unsupported discovery asset: {symbol}") from exc
            direction_rows: list[list[dict[str, Any]]] = []
            for direction in ("above", "below"):
                response = self._client.get(
                    f"{self.gamma_base}/public-search",
                    params={
                        "q": f"{name} {direction} {date_label}",
                        "events_status": "active",
                        "keep_closed_markets": 0,
                        "limit_per_type": query_limit,
                        "search_profiles": False,
                        "search_tags": False,
                    },
                )
                response.raise_for_status()
                direction_rows.append(
                    [
                        item
                        for item in _search_market_payloads(response.json())
                        if item.get("active") is not False
                        and item.get("closed") is not True
                    ]
                )

            bucket: list[dict[str, Any]] = []
            seen_for_asset: set[str] = set()
            for index in range(max((len(rows) for rows in direction_rows), default=0)):
                for rows in direction_rows:
                    if index >= len(rows):
                        continue
                    item = rows[index]
                    key = str(
                        item.get("id")
                        or item.get("conditionId")
                        or item.get("slug")
                        or ""
                    )
                    if not key or key in seen_for_asset:
                        continue
                    seen_for_asset.add(key)
                    bucket.append(item)
                    if len(bucket) >= per_asset:
                        break
                if len(bucket) >= per_asset:
                    break
            buckets.append(bucket)

        return _round_robin(buckets, limit=limit)

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

    def get_crypto_window_price(
        self,
        asset: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        response = self._client.get(
            f"{self.site_api_base}/crypto/crypto-price",
            params={
                "symbol": asset.upper(),
                "eventStartTime": _iso_z(start),
                "variant": interval_variant(interval),
                "endDate": _iso_z(end),
            },
        )
        response.raise_for_status()
        payload = response.json(parse_float=Decimal)
        if not isinstance(payload, dict):
            raise ValueError("unexpected Polymarket crypto-price response")
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


def _next_daily_target_date(now: datetime) -> date:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now_et = now.astimezone(NEW_YORK)
    target_date = now_et.date()
    deadline = datetime.combine(
        target_date,
        DAILY_SETTLEMENT_TIME,
        tzinfo=NEW_YORK,
    )
    return target_date + timedelta(days=1) if now_et >= deadline else target_date


def _round_robin(
    buckets: list[list[dict[str, Any]]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(max((len(bucket) for bucket in buckets), default=0)):
        for bucket in buckets:
            if index >= len(bucket):
                continue
            item = bucket[index]
            key = str(
                item.get("id")
                or item.get("conditionId")
                or item.get("slug")
                or ""
            )
            if not key or key in seen:
                continue
            seen.add(key)
            results.append(item)
            if len(results) >= limit:
                return results
    return results


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


def _iso_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
