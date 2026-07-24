"""Read-only settlement labels from the contract-authoritative Binance candle."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from sqlite3 import Row
from uuid import uuid4

from crypto_threshold.adapters.prices.binance import BinanceProvider
from crypto_threshold.domain.research import SettlementLabel
from crypto_threshold.storage.repositories import Repository

SETTLEMENT_SOURCE_VERSION = "binance-settlement-v1"


class SettlementService:
    """Create labels without using market outcomes or future model inputs."""

    def __init__(
        self,
        *,
        repository: Repository,
        binance: BinanceProvider,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.binance = binance
        self.clock = clock or (lambda: datetime.now(UTC))

    def settle_due(self, *, limit: int = 100) -> tuple[SettlementLabel, ...]:
        now = _utc(self.clock())
        rows = self.repository.settlement_candidates(
            ready_before=now - timedelta(minutes=1), limit=limit
        )
        return tuple(self._settle_rule(row, now=now) for row in rows)

    def settle_market(self, market_id: str) -> SettlementLabel:
        row = self.repository.get_resolution_rule(market_id)
        if row is None:
            raise ValueError(f"missing resolution rule for market: {market_id}")
        return self._settle_rule(row, now=_utc(self.clock()))

    def _settle_rule(self, row: Row, *, now: datetime) -> SettlementLabel:
        market_id = str(row["market_id"])
        target = _required_time(row, "target_time_utc")
        if now < target + timedelta(minutes=1):
            raise ValueError("settlement candle is not closed")
        _require_contract(row)

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
        )
        return self.repository.save_settlement_label(label)


def _require_contract(row: Row) -> None:
    expected = {
        "settlement_source": "Binance",
        "candle_interval": "1m",
        "price_field": "Close",
    }
    for field, value in expected.items():
        if str(row[field]).casefold() != value.casefold():
            raise ValueError(f"unsupported settlement {field}")
    if str(row["asset"]) not in {"BTC", "ETH"}:
        raise ValueError("unsupported settlement asset")
    if str(row["pair"]) not in {"BTC/USDT", "ETH/USDT"}:
        raise ValueError("unsupported settlement pair")
    if str(row["exact_operator"]) not in {">", "<"}:
        raise ValueError("unsupported settlement operator")
    if not bool(row["tradable"]):
        raise ValueError("preview-only contract cannot be labeled")


def _required_time(row: Row, field: str) -> datetime:
    value = row[field]
    if not value:
        raise ValueError(f"missing {field}")
    return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
