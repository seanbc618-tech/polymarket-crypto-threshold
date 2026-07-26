"""Read-only settlement labels from each contract's authoritative source."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from sqlite3 import Row
from typing import Any
from uuid import uuid4

from crypto_threshold.adapters.polymarket.base import PolymarketReadClient
from crypto_threshold.adapters.prices.binance import BinanceProvider
from crypto_threshold.domain.assets import SUPPORTED_ASSETS, asset_contract
from crypto_threshold.domain.research import SettlementLabel
from crypto_threshold.domain.rules import (
    DAILY_THRESHOLD_FAMILY,
    SHORT_UPDOWN_FAMILY,
    threshold_satisfied,
)
from crypto_threshold.storage.repositories import Repository

SETTLEMENT_SOURCE_VERSION = "binance-settlement-v1"
CHAINLINK_SETTLEMENT_SOURCE_VERSION = "chainlink-gamma-settlement-v1"
GAMMA_EVENT_SOURCE_VERSION = "gamma-event-v1"
PENDING_BACKOFF = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
    timedelta(hours=6),
)
ERROR_BACKOFF = timedelta(hours=1)


class SettlementPendingError(ValueError):
    """The authoritative public resolution payload is not complete yet."""


class SettlementBatchError(RuntimeError):
    """One or more settlement candidates failed after the batch was drained."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class SettlementService:
    """Create labels without using market outcomes or future model inputs."""

    def __init__(
        self,
        *,
        repository: Repository,
        binance: BinanceProvider,
        client: PolymarketReadClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.binance = binance
        self.client = client
        self.clock = clock or (lambda: datetime.now(UTC))

    def settle_due(self, *, limit: int = 100) -> tuple[SettlementLabel, ...]:
        now = _utc(self.clock())
        rows = self.repository.settlement_candidates(
            ready_before=now - timedelta(minutes=1), limit=limit
        )
        labels: list[SettlementLabel] = []
        errors: list[str] = []
        for row in rows:
            try:
                labels.append(self._settle_with_attempt(row, now=now))
            except SettlementPendingError:
                continue
            except Exception as exc:
                errors.append(f"{row['market_id']}:{type(exc).__name__}")
        if errors:
            raise SettlementBatchError(tuple(errors))
        return tuple(labels)

    def settle_market(self, market_id: str) -> SettlementLabel:
        row = self.repository.get_resolution_rule(market_id)
        if row is None:
            raise ValueError(f"missing resolution rule for market: {market_id}")
        return self._settle_with_attempt(row, now=_utc(self.clock()))

    def _settle_with_attempt(self, row: Row, *, now: datetime) -> SettlementLabel:
        market_id = str(row["market_id"])
        attempt_count = self.repository.start_settlement_attempt(
            market_id=market_id,
            target_time_utc=_required_time(row, "target_time_utc"),
            contract_family=str(row["contract_family"] or DAILY_THRESHOLD_FAMILY),
            attempted_at=now,
        )
        try:
            label = self._settle_rule(row, now=now)
        except SettlementPendingError as exc:
            self.repository.finish_settlement_attempt(
                market_id=market_id,
                status="pending",
                next_attempt_at=now + _pending_delay(attempt_count),
                reason=_short_reason(exc),
                updated_at=now,
            )
            raise
        except Exception as exc:
            self.repository.finish_settlement_attempt(
                market_id=market_id,
                status="error",
                next_attempt_at=now + ERROR_BACKOFF,
                reason=_short_reason(exc),
                updated_at=now,
            )
            raise
        self.repository.finish_settlement_attempt(
            market_id=market_id,
            status="succeeded",
            next_attempt_at=now,
            reason=None,
            updated_at=now,
        )
        return label

    def _settle_rule(self, row: Row, *, now: datetime) -> SettlementLabel:
        family = str(row["contract_family"] or DAILY_THRESHOLD_FAMILY)
        if family == SHORT_UPDOWN_FAMILY:
            return self._settle_short_updown(row, now=now)
        if family != DAILY_THRESHOLD_FAMILY:
            raise ValueError(f"unsupported settlement contract family: {family}")
        return self._settle_daily_threshold(row, now=now)

    def _settle_daily_threshold(self, row: Row, *, now: datetime) -> SettlementLabel:
        market_id = str(row["market_id"])
        target = _required_time(row, "target_time_utc")
        if now < target + timedelta(minutes=1):
            raise ValueError("settlement candle is not closed")
        _require_daily_contract(row)

        series = self.binance.get_klines(
            str(row["asset"]),
            interval="1m",
            limit=1,
            start_time=target,
            end_time=target + timedelta(minutes=1) - timedelta(milliseconds=1),
        )
        received_at = _utc(series.received_at)
        payload_id = self.repository.record_external_payload(
            market_id=market_id,
            source="binance",
            payload_kind="settlement_candle_1m_close",
            payload=series.raw_payload,
            observed_at=series.klines[0].close_time if series.klines else None,
            received_at=received_at,
            source_version=series.source_version,
        )
        if series.provider != "binance":
            raise ValueError("settlement provider mismatch")
        if series.symbol != str(row["pair"]).replace("/", ""):
            raise ValueError("settlement pair mismatch")
        if series.interval != "1m":
            raise ValueError("settlement candle interval mismatch")
        if len(series.klines) != 1:
            raise ValueError("settlement response must contain exactly one candle")
        candle = series.klines[0]
        if _utc(candle.open_time) != target:
            raise ValueError("settlement candle open time mismatch")
        if _utc(candle.close_time) > now:
            raise ValueError("settlement candle is not closed")

        strike = Decimal(str(row["strike"]))
        operator = str(row["exact_operator"])
        outcome_yes = candle.close > strike if operator == ">" else candle.close < strike
        label = SettlementLabel(
            label_id=f"label:{uuid4()}",
            market_id=market_id,
            target_time_utc=target,
            provider="binance",
            pair=str(row["pair"]),
            candle_interval="1m",
            price_field="Close",
            exact_operator=operator,
            strike=strike,
            observed_value=candle.close,
            outcome_yes=outcome_yes,
            payload_id=payload_id,
            observed_at=_utc(candle.close_time),
            received_at=received_at,
            source_version=SETTLEMENT_SOURCE_VERSION,
            contract_family=DAILY_THRESHOLD_FAMILY,
        )
        return self.repository.save_settlement_label(label)

    def _settle_short_updown(self, row: Row, *, now: datetime) -> SettlementLabel:
        market_id = str(row["market_id"])
        target = _required_time(row, "target_time_utc")
        if now < target:
            raise ValueError("settlement boundary has not passed")
        _require_short_contract(row)
        if self.client is None:
            raise ValueError("Gamma client is required for short Up/Down settlement")
        event_id = str(row["event_id"] or "")
        if not event_id:
            raise ValueError("missing event_id for short Up/Down settlement")

        event = self.client.get_event(event_id)
        received_at = _utc(self.clock())
        resolved_market = _event_market(
            event,
            market_id=market_id,
            condition_id=str(row["condition_id"] or ""),
        )
        payload_id, _ = self.repository.record_settlement_payload_if_changed(
            market_id=market_id,
            source="gamma",
            payload_kind="chainlink_resolution_event",
            payload=event,
            observed_at=target,
            received_at=received_at,
            source_version=GAMMA_EVENT_SOURCE_VERSION,
            payload_fingerprint=_resolution_fingerprint(event, resolved_market),
        )
        if resolved_market is None:
            raise SettlementPendingError("market missing from Gamma resolution event")
        if resolved_market.get("closed") is not True:
            raise SettlementPendingError("Gamma market is not resolved")

        metadata = _mapping(
            event.get("eventMetadata")
            or resolved_market.get("eventMetadata")
            or event.get("metadata")
        )
        start_price = _positive_decimal(metadata.get("priceToBeat"))
        final_price = _positive_decimal(metadata.get("finalPrice"))
        if start_price is None or final_price is None:
            raise SettlementPendingError(
                "Chainlink priceToBeat/finalPrice is not published"
            )
        resolved_up = _resolved_affirmative_outcome(resolved_market)
        computed_up = threshold_satisfied(final_price, start_price, ">=")
        if resolved_up is None:
            raise SettlementPendingError("Gamma Up/Down outcome is not final")
        if resolved_up != computed_up:
            raise ValueError("Gamma outcome disagrees with Chainlink boundary prices")

        label = SettlementLabel(
            label_id=f"label:{uuid4()}",
            market_id=market_id,
            target_time_utc=target,
            provider="chainlink",
            pair=str(row["pair"]),
            candle_interval=str(row["candle_interval"]),
            price_field="data_stream_value",
            exact_operator=">=",
            strike=start_price,
            observed_value=final_price,
            outcome_yes=computed_up,
            payload_id=payload_id,
            observed_at=target,
            received_at=received_at,
            source_version=CHAINLINK_SETTLEMENT_SOURCE_VERSION,
            contract_family=SHORT_UPDOWN_FAMILY,
        )
        return self.repository.save_settlement_label(label)


def _pending_delay(attempt_count: int) -> timedelta:
    index = max(0, min(attempt_count - 1, len(PENDING_BACKOFF) - 1))
    return PENDING_BACKOFF[index]


def _short_reason(error: Exception) -> str:
    reason = str(error).strip() or type(error).__name__
    return reason[:240]


def _resolution_fingerprint(
    event: dict[str, Any],
    market: dict[str, Any] | None,
) -> str:
    metadata = _mapping(
        event.get("eventMetadata")
        or (market or {}).get("eventMetadata")
        or event.get("metadata")
    )
    projection = {
        "event_closed": event.get("closed"),
        "market_id": (market or {}).get("id"),
        "condition_id": (market or {}).get("conditionId"),
        "market_closed": (market or {}).get("closed"),
        "price_to_beat": metadata.get("priceToBeat"),
        "final_price": metadata.get("finalPrice"),
        "outcomes": _listish((market or {}).get("outcomes")),
        "outcome_prices": _listish((market or {}).get("outcomePrices")),
    }
    return json.dumps(projection, default=str, sort_keys=True, separators=(",", ":"))


def _require_daily_contract(row: Row) -> None:
    expected = {
        "settlement_source": "Binance",
        "candle_interval": "1m",
        "price_field": "Close",
    }
    for field, value in expected.items():
        if str(row[field]).casefold() != value.casefold():
            raise ValueError(f"unsupported settlement {field}")
    asset = str(row["asset"])
    if asset not in SUPPORTED_ASSETS:
        raise ValueError("unsupported settlement asset")
    if str(row["pair"]) != asset_contract(asset).binance_pair:
        raise ValueError("unsupported settlement pair")
    if str(row["exact_operator"]) not in {">", "<"}:
        raise ValueError("unsupported settlement operator")
    keys = set(row.keys())
    had_analyzed_predeadline_signal = (
        bool(row["had_analyzed_predeadline_signal"])
        if "had_analyzed_predeadline_signal" in keys
        else False
    )
    if not bool(row["tradable"]) and not had_analyzed_predeadline_signal:
        raise ValueError("preview-only contract cannot be labeled")


def _require_short_contract(row: Row) -> None:
    expected = {
        "settlement_source": "chainlink",
        "price_field": "data_stream_value",
        "exact_operator": ">=",
        "timezone": "UTC",
        "observation_time": "window_start",
        "boundary_type": "window_start_price",
        "affirmative_outcome": "Up",
        "negative_outcome": "Down",
    }
    for field, value in expected.items():
        if str(row[field]).casefold() != value.casefold():
            raise ValueError(f"unsupported settlement {field}")
    if str(row["candle_interval"]) not in {"5m", "15m"}:
        raise ValueError("unsupported settlement candle_interval")
    asset = str(row["asset"])
    contract = asset_contract(asset)
    if not contract.short_updown or str(row["pair"]) != contract.chainlink_pair:
        raise ValueError("unsupported Chainlink settlement identity")
    keys = set(row.keys())
    had_analyzed_predeadline_signal = (
        bool(row["had_analyzed_predeadline_signal"])
        if "had_analyzed_predeadline_signal" in keys
        else False
    )
    if not bool(row["tradable"]) and not had_analyzed_predeadline_signal:
        raise ValueError("preview-only contract cannot be labeled")


def _event_market(
    event: dict[str, Any],
    *,
    market_id: str,
    condition_id: str,
) -> dict[str, Any] | None:
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        identifiers = {
            str(market.get("id") or ""),
            str(market.get("conditionId") or ""),
        }
        if market_id in identifiers or (condition_id and condition_id in identifiers):
            return market
    return None


def _resolved_affirmative_outcome(market: dict[str, Any]) -> bool | None:
    outcomes = [str(value).strip().lower() for value in _listish(market.get("outcomes"))]
    prices = [_decimal(value) for value in _listish(market.get("outcomePrices"))]
    if len(outcomes) != 2 or len(prices) != 2 or set(outcomes) != {"up", "down"}:
        return None
    up = prices[outcomes.index("up")]
    down = prices[outcomes.index("down")]
    if up == Decimal("1") and down == Decimal("0"):
        return True
    if up == Decimal("0") and down == Decimal("1"):
        return False
    return None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _listish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _required_time(row: Row, field: str) -> datetime:
    value = row[field]
    if not value:
        raise ValueError(f"missing {field}")
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
