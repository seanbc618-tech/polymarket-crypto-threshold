"""R0 checkpoint baseline and real public-book latency replay tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from crypto_threshold.adapters.polymarket.translator import translate_order_book
from crypto_threshold.domain.probability import AnalysisSignal
from crypto_threshold.domain.research import SettlementLabel
from crypto_threshold.domain.rules import SHORT_UPDOWN_FAMILY, CryptoResolutionRule
from crypto_threshold.services.schema_drift_service import ExternalPayloadSchemaMonitor
from crypto_threshold.services.short_challenger_service import ShortChallengerService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository

TARGET = datetime(2026, 7, 30, 12, 5, tzinfo=UTC)
DECISION = TARGET - timedelta(minutes=3)


class _LatencyClient:
    def __init__(self, monotonic_state: list[float]) -> None:
        self.monotonic_state = monotonic_state
        self.book_reads = 0

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        self.book_reads += 1
        self.monotonic_state[0] += 0.01
        return _book_payload(DECISION)


def test_r0_records_baseline_latency_grid_idempotently_and_settles(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "challenger.db")
    database.initialize()
    repository = Repository(database)
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO markets (market_id, question) VALUES ('m1', 'BTC Up or Down?')"
        )

    signal = _signal()
    repository.save_analysis_signal(signal)
    for token_id, outcome in (("up-token", "UP"), ("down-token", "DOWN")):
        repository.save_market_snapshot(
            translate_order_book(
                market_id="m1",
                token_id=token_id,
                outcome=outcome,
                payload=_book_payload(DECISION),
                received_at=DECISION,
            )
        )

    monotonic_state = [0.0]

    def sleeper(seconds: float) -> None:
        monotonic_state[0] += seconds

    client = _LatencyClient(monotonic_state)
    service = ShortChallengerService(
        repository,
        client,  # type: ignore[arg-type]
        min_net_ev=Decimal("0.02"),
        checkpoints_seconds=(180, 120, 60, 30),
        latencies_ms=(0, 100, 250, 500, 1000),
        max_book_age_seconds=90,
        clock=lambda: DECISION,
        sleeper=sleeper,
        monotonic=lambda: monotonic_state[0],
    )
    boundary = repository.max_external_payload_id()

    first = service.record(_signal(), _rule(), checkpoint_lead_seconds=180)
    duplicate = service.record(_signal(), _rule(), checkpoint_lead_seconds=180)

    assert first.observation_created
    assert first.replay_created_count == 5
    assert first.paper_entered_count == 5
    assert not duplicate.observation_created
    assert duplicate.replay_created_count == 0
    assert client.book_reads == 5
    assert repository.table_count("short_challenger_observations") == 1
    assert repository.table_count("short_latency_replays") == 5
    with database.connect() as connection:
        observation = connection.execute(
            "SELECT * FROM short_challenger_observations"
        ).fetchone()
        replays = connection.execute(
            "SELECT * FROM short_latency_replays ORDER BY latency_ms"
        ).fetchall()
    assert observation["checkpoint_lead_seconds"] == 180
    assert observation["market_yes_midpoint"] == "0.49"
    assert observation["market_yes_ask_vwap"] == "0.50"
    assert observation["yes_ask_depth"] == "50.00"
    assert [row["latency_ms"] for row in replays] == [0, 100, 250, 500, 1000]
    assert all(row["action"] == "enter" for row in replays)
    assert all(row["status"] == "open" for row in replays)
    assert all(row["payload_id"] is not None for row in replays)
    assert all(int(row["actual_latency_ms"]) >= int(row["latency_ms"]) for row in replays)
    assert ExternalPayloadSchemaMonitor(repository).inspect_after(boundary).status == "ok"

    payload_id = repository.record_external_payload(
        market_id="m1",
        source="polymarket_site",
        payload_kind="authoritative_window_price",
        payload={"completed": True},
        observed_at=TARGET,
        received_at=TARGET,
        source_version="test",
    )
    repository.save_settlement_label(
        SettlementLabel(
            label_id="label-1",
            market_id="m1",
            target_time_utc=TARGET,
            provider="chainlink",
            pair="BTC/USD",
            candle_interval="5m",
            price_field="data_stream_value",
            exact_operator=">=",
            strike=Decimal("100"),
            observed_value=Decimal("101"),
            outcome_yes=True,
            payload_id=payload_id,
            observed_at=TARGET,
            received_at=TARGET,
            source_version="test",
            contract_family=SHORT_UPDOWN_FAMILY,
        )
    )

    assert service.settle_open() == 5
    with database.connect() as connection:
        settled = connection.execute(
            "SELECT status, outcome_yes, pnl_usdc FROM short_latency_replays"
        ).fetchall()
    assert all(row["status"] == "settled" for row in settled)
    assert all(row["outcome_yes"] == 1 for row in settled)
    assert all(Decimal(str(row["pnl_usdc"])) > 0 for row in settled)


def _signal() -> AnalysisSignal:
    return AnalysisSignal(
        signal_id="signal-r0",
        market_id="m1",
        asset="BTC",
        threshold=None,
        deadline=TARGET,
        estimated_probability=Decimal("0.80"),
        probability_low=Decimal("0.75"),
        probability_high=Decimal("0.85"),
        yes_midpoint=Decimal("0.49"),
        no_midpoint=Decimal("0.49"),
        yes_ask_vwap=Decimal("0.50"),
        no_ask_vwap=Decimal("0.51"),
        target_size_usdc=Decimal("10"),
        fee_rate=Decimal("0.07"),
        yes_fee_per_share=Decimal("0.0175"),
        no_fee_per_share=Decimal("0.0175"),
        yes_spread_cost=Decimal("0.01"),
        no_spread_cost=Decimal("0.02"),
        yes_slippage_cost=Decimal("0"),
        no_slippage_cost=Decimal("0"),
        yes_net_ev=Decimal("0.2325"),
        no_net_ev=Decimal("-0.3775"),
        selected_outcome="YES",
        net_ev=Decimal("0.2325"),
        status="analyzed",
        model_name="cex_kline_chainlink_direction",
        model_version="cex-kline-chainlink-direction-v1+test",
        confidence="high",
        reasons=("closed_cex_checkpoint:T-180s",),
        observed_at=DECISION,
        received_at=DECISION,
        source_version="market-workflow-v4",
        contract_family=SHORT_UPDOWN_FAMILY,
        affirmative_outcome="Up",
        negative_outcome="Down",
    )


def _rule() -> CryptoResolutionRule:
    return CryptoResolutionRule(
        event_id="event-1",
        condition_id="condition-1",
        yes_token_id="up-token",
        no_token_id="down-token",
        asset="BTC",
        settlement_provider="chainlink",
        pair="BTC/USD",
        exact_operator=">=",
        strike=Decimal("0"),
        candle_interval="5m",
        price_field="data_stream_value",
        timezone="UTC",
        observation_time=None,
        target_time_utc=TARGET,
        gamma_end_date=TARGET,
        parser_version="test",
        raw_description="test",
        question="BTC Up or Down?",
        rule_confidence=1.0,
        tradable=True,
        preview_only=False,
        rejection_reasons=(),
        contract_family=SHORT_UPDOWN_FAMILY,
        boundary_type="window_start_price",
        window_start_time_utc=TARGET - timedelta(minutes=5),
        affirmative_outcome="Up",
        negative_outcome="Down",
    )


def _book_payload(observed_at: datetime) -> dict[str, Any]:
    return {
        "timestamp": str(int(observed_at.timestamp() * 1000)),
        "bids": [{"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.50", "size": "100"}],
    }
