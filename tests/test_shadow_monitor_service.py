"""Persistent read-only shadow cycle tests."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from crypto_threshold.adapters.prices.stream import ReferencePriceTick
from crypto_threshold.config import Settings
from crypto_threshold.services.discovery_service import DiscoveryService
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.paper_ledger_service import PaperLedgerService
from crypto_threshold.services.shadow_monitor_service import ShadowMonitorService
from crypto_threshold.services.stream_research_service import StreamPulseResult
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository
from tests.conftest import (
    NOW,
    FakeBinanceProvider,
    FakeCoinbaseProvider,
    FakePolymarketClient,
)


def test_once_runs_with_streams_disabled_and_persists_cycle_and_paper(
    tmp_path: Path, market_payload: dict[str, object]
) -> None:
    database = Database(tmp_path / "shadow.db")
    database.initialize()
    repository = Repository(database)
    client = FakePolymarketClient(market_payload)
    settings = Settings(DATABASE_PATH=str(database.path), _env_file=None)
    workflow = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=FakeCoinbaseProvider(),
        settings=settings,
        clock=lambda: NOW,
    )
    monitor = ShadowMonitorService(
        repository=repository,
        discovery=DiscoveryService(client, repository, clock=lambda: NOW),
        workflow=workflow,
        paper=PaperLedgerService(
            repository, min_net_ev=Decimal("0"), clock=lambda: NOW
        ),
        discovery_limit=10,
        analysis_limit=10,
        clock=lambda: NOW,
    )
    monitor.start()
    cycle = monitor.run_once()
    monitor.stop()
    assert cycle.status == "complete_rest_fallback"
    assert cycle.discovered_count == 1
    assert cycle.analyzed_count == 1
    assert cycle.paper_entered_count + cycle.paper_skipped_count == 1
    assert cycle.stream_health["polymarket"] == {"status": "disabled"}
    assert cycle.stream_health["binance_reference"] == {"status": "disabled"}
    schema_health = cycle.stream_health["schema_drift"]
    assert schema_health["status"] == "ok"
    assert schema_health["scanned_payload_count"] == 8
    assert schema_health["issues"] == []
    assert repository.table_count("shadow_cycles") == 1
    assert repository.table_count("paper_ledger") == 1
    assert not hasattr(workflow, "place_order")


def test_fresh_stream_hint_selects_market_but_rest_workflow_remains_authority(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "hint.db")
    database.initialize()
    repository = Repository(database)
    results = [
        SimpleNamespace(
            market=SimpleNamespace(market_id="btc-market"),
            rule=SimpleNamespace(tradable=True, asset="BTC"),
        ),
        SimpleNamespace(
            market=SimpleNamespace(market_id="eth-market"),
            rule=SimpleNamespace(tradable=True, asset="ETH"),
        ),
    ]

    class Discovery:
        def discover(self, *, limit: int) -> list[object]:
            return results[:limit]

    class Workflow:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def analyze(self, market_id: str) -> object:
            self.calls.append(market_id)
            return SimpleNamespace()

    class Paper:
        def record(self, signal: object) -> tuple[object, bool]:
            return SimpleNamespace(action="skip"), True

        def settle_open(self) -> int:
            return 0

    class Coordinator:
        def sync_subscriptions(self, **kwargs: object) -> None:
            return None

        def pulse(self) -> StreamPulseResult:
            return StreamPulseResult(
                status="connected",
                reprice_ladders=(),
                reprice_market_ids=("eth-market",),
                reconcile_due=False,
                rest_fallback_active=False,
                health={"status": "connected"},
            )

        def start(self) -> bool:
            return True

        def stop(self) -> None:
            return None

    class ReferenceStream:
        def health(self) -> dict[str, object]:
            return {"status": "connected", "detail": {"generation": 2}}

        def drain(self) -> tuple[ReferencePriceTick, ...]:
            return (
                ReferencePriceTick(
                    provider="binance",
                    pair="ETHUSDT",
                    candle_interval="1m",
                    price_field="Close",
                    price=Decimal("3700"),
                    provider_timestamp=NOW,
                    received_at=NOW,
                    fresh=True,
                    sequence="1:1",
                    payload_hash="0" * 64,
                ),
            )

    workflow = Workflow()
    monitor = ShadowMonitorService(
        repository=repository,
        discovery=Discovery(),  # type: ignore[arg-type]
        workflow=workflow,  # type: ignore[arg-type]
        paper=Paper(),  # type: ignore[arg-type]
        stream_coordinator=Coordinator(),  # type: ignore[arg-type]
        reference_stream=ReferenceStream(),  # type: ignore[arg-type]
        discovery_limit=10,
        analysis_limit=10,
        clock=lambda: NOW,
    )
    cycle = monitor.run_once()
    assert workflow.calls == ["eth-market"]
    assert cycle.analyzed_count == 1
    reference_health = cycle.stream_health["binance_reference"]
    assert reference_health["drained_ticks"] == 1
    assert reference_health["drained_tick_evidence"] == [
        {
            "provider": "binance",
            "pair": "ETHUSDT",
            "candle_interval": "1m",
            "price_field": "Close",
            "provider_timestamp": NOW.isoformat(),
            "received_at": NOW.isoformat(),
            "fresh": True,
            "sequence": "1:1",
            "payload_hash": "0" * 64,
            "source_version": "binance-spot-sdk-stream-v1",
        }
    ]


def test_payload_schema_drift_degrades_cycle_without_exchange_mutation(
    tmp_path: Path,
    market_payload: dict[str, object],
) -> None:
    class DriftedCoinbase(FakeCoinbaseProvider):
        def get_spot_price(self, asset: str):
            snapshot = super().get_spot_price(asset)
            return replace(snapshot, raw_payload={"amount": "104900"})

    database = Database(tmp_path / "drift.db")
    database.initialize()
    repository = Repository(database)
    client = FakePolymarketClient(market_payload)
    workflow = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=FakeBinanceProvider(),
        coinbase=DriftedCoinbase(),
        settings=Settings(DATABASE_PATH=str(database.path), _env_file=None),
        clock=lambda: NOW,
    )
    monitor = ShadowMonitorService(
        repository=repository,
        discovery=DiscoveryService(client, repository, clock=lambda: NOW),
        workflow=workflow,
        paper=PaperLedgerService(
            repository,
            min_net_ev=Decimal("0"),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )

    cycle = monitor.run_once()

    assert cycle.status == "degraded"
    assert cycle.stream_health["schema_drift"]["status"] == "drift_detected"
    assert any(
        reason == "schema_drift:coinbase/sanity_spot:data_not_object"
        for reason in cycle.reasons
    )
    assert not hasattr(workflow, "place_order")
