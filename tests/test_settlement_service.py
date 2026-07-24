"""Authoritative Binance candle settlement-label tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_threshold.domain.prices import Kline, KlineSeries
from crypto_threshold.services.discovery_service import DiscoveryService
from crypto_threshold.services.settlement_service import SettlementService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import NOW, TARGET, FakePolymarketClient, make_market_payload


class SettlementBinance:
    def __init__(self, close: Decimal) -> None:
        self.close = close

    def get_klines(
        self,
        asset: str,
        interval: str,
        limit: int,
        *,
        start_time: object,
        end_time: object,
    ) -> KlineSeries:
        assert (asset, interval, limit) == ("BTC", "1m", 1)
        assert start_time == TARGET
        assert end_time == TARGET + timedelta(minutes=1) - timedelta(milliseconds=1)
        candle = Kline(
            open_time=TARGET,
            close_time=TARGET + timedelta(minutes=1) - timedelta(milliseconds=1),
            open=self.close,
            high=self.close,
            low=self.close,
            close=self.close,
            volume=Decimal("1"),
        )
        return KlineSeries(
            asset="BTC",
            quote="USDT",
            provider="binance",
            symbol="BTCUSDT",
            interval="1m",
            klines=(candle,),
            received_at=TARGET + timedelta(minutes=2),
            source_version="binance-test-v1",
            raw_payload=[[int(TARGET.timestamp() * 1000), str(self.close)]],
        )


@pytest.mark.parametrize(
    ("direction", "close", "expected"),
    [
        ("above", Decimal("100000"), False),
        ("above", Decimal("100001"), True),
        ("below", Decimal("100000"), False),
        ("below", Decimal("99999"), True),
    ],
)
def test_strict_settlement_boundary_and_raw_payload_order(
    tmp_path: Path, direction: str, close: Decimal, expected: bool
) -> None:
    database = Database(tmp_path / f"{direction}-{close}.db")
    database.initialize()
    repository = Repository(database)
    payload = make_market_payload(
        question=f"Will Bitcoin be {direction} $100,000 on July 23, 2026?",
    )
    DiscoveryService(
        FakePolymarketClient(payload), repository, clock=lambda: NOW
    ).discover()
    label = SettlementService(
        repository=repository,
        binance=SettlementBinance(close),  # type: ignore[arg-type]
        clock=lambda: TARGET + timedelta(minutes=2),
    ).settle_market("market-1")
    assert label.outcome_yes is expected
    with database.connect() as connection:
        row = connection.execute(
            "SELECT payload_id, created_at FROM settlement_labels"
        ).fetchone()
        payload_row = connection.execute(
            "SELECT id, created_at FROM external_payloads WHERE id = ?",
            (row["payload_id"],),
        ).fetchone()
    assert payload_row["id"] == label.payload_id
    assert payload_row["created_at"] <= row["created_at"]


def test_unclosed_settlement_candle_rejects_before_network(tmp_path: Path) -> None:
    database = Database(tmp_path / "unclosed.db")
    database.initialize()
    repository = Repository(database)
    payload = make_market_payload()
    DiscoveryService(
        FakePolymarketClient(payload), repository, clock=lambda: NOW
    ).discover()
    with pytest.raises(ValueError, match="not closed"):
        SettlementService(
            repository=repository,
            binance=SettlementBinance(Decimal("100001")),  # type: ignore[arg-type]
            clock=lambda: TARGET + timedelta(seconds=30),
        ).settle_market("market-1")
    assert repository.table_count("settlement_labels") == 0
