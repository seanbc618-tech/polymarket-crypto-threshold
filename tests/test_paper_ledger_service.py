"""Persistent paper ledger policy and idempotency tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_threshold.domain.probability import AnalysisSignal
from crypto_threshold.domain.research import SettlementLabel
from crypto_threshold.services.paper_ledger_service import PaperLedgerService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_paper_entry_is_idempotent_and_settles_from_label(tmp_path: Path) -> None:
    database = Database(tmp_path / "paper.db")
    database.initialize()
    repository = Repository(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO markets (market_id, question) VALUES ('m1', 'paper')"
        )
    signal = _signal("s1", net_ev=Decimal("0.10"))
    repository.save_analysis_signal(signal)
    service = PaperLedgerService(
        repository, min_net_ev=Decimal("0.02"), clock=lambda: NOW
    )
    entry, created = service.record(signal)
    duplicate, duplicate_created = service.record(signal)
    assert created
    assert entry.action == "enter"
    assert entry.status == "open"
    assert duplicate.entry_id == entry.entry_id
    assert not duplicate_created
    assert repository.table_count("paper_ledger") == 1

    payload_id = repository.record_external_payload(
        market_id="m1",
        source="binance",
        payload_kind="settlement",
        payload={},
        observed_at=NOW + timedelta(days=1),
        received_at=NOW + timedelta(days=1),
        source_version="test",
    )
    repository.save_settlement_label(
        SettlementLabel(
            label_id="label-1",
            market_id="m1",
            target_time_utc=NOW + timedelta(days=1),
            provider="binance",
            pair="BTC/USDT",
            candle_interval="1m",
            price_field="Close",
            exact_operator=">",
            strike=Decimal("100"),
            observed_value=Decimal("101"),
            outcome_yes=True,
            payload_id=payload_id,
            observed_at=NOW + timedelta(days=1),
            received_at=NOW + timedelta(days=1),
            source_version="test",
        )
    )
    assert service.settle_open() == 1
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM paper_ledger").fetchone()
    assert row["status"] == "settled"
    assert Decimal(row["payout_usdc"]) == Decimal("20")
    assert Decimal(row["pnl_usdc"]) == Decimal("9.8")


def test_paper_policy_records_skip_below_threshold(tmp_path: Path) -> None:
    database = Database(tmp_path / "skip.db")
    database.initialize()
    repository = Repository(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO markets (market_id, question) VALUES ('m1', 'paper')"
        )
    signal = _signal("s1", net_ev=Decimal("0.01"))
    repository.save_analysis_signal(signal)
    entry, _created = PaperLedgerService(
        repository, min_net_ev=Decimal("0.02"), clock=lambda: NOW
    ).record(signal)
    assert entry.action == "skip"
    assert entry.status == "skipped"
    assert "net_ev_below_paper_threshold" in entry.reasons


def test_labeled_entry_is_not_starved_by_older_unlabeled_open_row(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "paper-backlog.db")
    database.initialize()
    repository = Repository(database)
    with database.transaction() as connection:
        connection.executemany(
            "INSERT INTO markets (market_id, question) VALUES (?, 'paper')",
            (("m1",), ("m2",)),
        )

    older = _signal("s1", net_ev=Decimal("0.10"))
    newer = replace(
        _signal("s2", net_ev=Decimal("0.10")),
        market_id="m2",
        observed_at=NOW + timedelta(minutes=1),
        received_at=NOW + timedelta(minutes=1),
    )
    repository.save_analysis_signal(older)
    repository.save_analysis_signal(newer)
    service = PaperLedgerService(
        repository,
        min_net_ev=Decimal("0.02"),
        clock=lambda: NOW + timedelta(minutes=2),
    )
    service.record(older)
    service.record(newer)

    payload_id = repository.record_external_payload(
        market_id="m2",
        source="binance",
        payload_kind="settlement",
        payload={},
        observed_at=NOW + timedelta(days=1),
        received_at=NOW + timedelta(days=1),
        source_version="test",
    )
    repository.save_settlement_label(
        SettlementLabel(
            label_id="label-2",
            market_id="m2",
            target_time_utc=NOW + timedelta(days=1),
            provider="binance",
            pair="BTC/USDT",
            candle_interval="1m",
            price_field="Close",
            exact_operator=">",
            strike=Decimal("100"),
            observed_value=Decimal("99"),
            outcome_yes=False,
            payload_id=payload_id,
            observed_at=NOW + timedelta(days=1),
            received_at=NOW + timedelta(days=1),
            source_version="test",
        )
    )

    assert service.settle_open(limit=1) == 1
    with database.connect() as connection:
        statuses = dict(
            connection.execute(
                "SELECT market_id, status FROM paper_ledger ORDER BY market_id"
            )
        )
    assert statuses == {"m1": "open", "m2": "settled"}


def _signal(signal_id: str, *, net_ev: Decimal) -> AnalysisSignal:
    return AnalysisSignal(
        signal_id=signal_id,
        market_id="m1",
        asset="BTC",
        threshold=Decimal("100"),
        deadline=NOW + timedelta(days=1),
        estimated_probability=Decimal("0.7"),
        probability_low=Decimal("0.6"),
        probability_high=Decimal("0.8"),
        yes_midpoint=Decimal("0.49"),
        no_midpoint=Decimal("0.49"),
        yes_ask_vwap=Decimal("0.5"),
        no_ask_vwap=Decimal("0.5"),
        target_size_usdc=Decimal("10"),
        fee_rate=Decimal("0.01"),
        yes_fee_per_share=Decimal("0.01"),
        no_fee_per_share=Decimal("0.01"),
        yes_spread_cost=Decimal("0.01"),
        no_spread_cost=Decimal("0.01"),
        yes_slippage_cost=Decimal("0"),
        no_slippage_cost=Decimal("0"),
        yes_net_ev=net_ev,
        no_net_ev=Decimal("-0.2"),
        selected_outcome="YES",
        net_ev=net_ev,
        status="analyzed",
        model_name="test",
        model_version="test",
        confidence="test",
        reasons=(),
        observed_at=NOW,
        received_at=NOW,
    )
