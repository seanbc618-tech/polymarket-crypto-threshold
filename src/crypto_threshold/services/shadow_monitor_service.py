"""Serial read-only realtime shadow monitor."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from crypto_threshold.adapters.prices.stream import BinanceReferencePriceStream
from crypto_threshold.domain.research import ShadowCycleResult
from crypto_threshold.services.discovery_service import DiscoveryService
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.paper_ledger_service import PaperLedgerService
from crypto_threshold.services.schema_drift_service import ExternalPayloadSchemaMonitor
from crypto_threshold.services.settlement_service import SettlementService
from crypto_threshold.services.stream_research_service import StreamResearchCoordinator
from crypto_threshold.storage.repositories import Repository

SHADOW_SOURCE_VERSION = "shadow-monitor-v1"


class ShadowMonitorService:
    """Keep all orchestration on the main thread; streams only provide hints."""

    def __init__(
        self,
        *,
        repository: Repository,
        discovery: DiscoveryService,
        workflow: MarketWorkflowService,
        paper: PaperLedgerService,
        settlement: SettlementService | None = None,
        stream_coordinator: StreamResearchCoordinator | None = None,
        reference_stream: BinanceReferencePriceStream | None = None,
        schema_monitor: ExternalPayloadSchemaMonitor | None = None,
        discovery_limit: int = 20,
        analysis_limit: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.discovery = discovery
        self.workflow = workflow
        self.paper = paper
        self.settlement = settlement
        self.stream_coordinator = stream_coordinator
        self.reference_stream = reference_stream
        self.schema_monitor = schema_monitor or ExternalPayloadSchemaMonitor(repository)
        self.discovery_limit = discovery_limit
        self.analysis_limit = analysis_limit
        self.clock = clock or (lambda: datetime.now(UTC))

    def start(self) -> None:
        if self.stream_coordinator is not None:
            self.stream_coordinator.start()
        if self.reference_stream is not None:
            self.reference_stream.start()

    def stop(self) -> None:
        if self.reference_stream is not None:
            self.reference_stream.stop()
        if self.stream_coordinator is not None:
            self.stream_coordinator.stop()

    def run_once(self) -> ShadowCycleResult:
        started_at = _utc(self.clock())
        reasons: list[str] = []
        analyzed = 0
        entered = 0
        skipped = 0
        discovered = 0
        rest_fallback = True
        stream_health: dict[str, Any] = {}
        payload_boundary = self.schema_monitor.capture_boundary()
        try:
            results = self.discovery.discover(limit=self.discovery_limit)
            discovered = len(results)
            eligible = {
                result.market.market_id: result
                for result in results
                if result.rule.tradable
            }
            target_market_ids: list[str] = []
            rest_fallback = self.stream_coordinator is None
            if self.stream_coordinator is not None:
                self.stream_coordinator.sync_subscriptions(
                    active_market_ids=tuple(eligible)
                )
                pulse = self.stream_coordinator.pulse()
                stream_health["polymarket"] = dict(pulse.health)
                rest_fallback = pulse.rest_fallback_active
                target_market_ids.extend(pulse.reprice_market_ids)
                if pulse.reconcile_due:
                    reasons.append("reconciliation_hint_pending_rest")
            else:
                stream_health["polymarket"] = {"status": "disabled"}

            if self.reference_stream is not None:
                reference_health = dict(self.reference_stream.health())
                ticks = self.reference_stream.drain()
                reference_health["drained_ticks"] = len(ticks)
                reference_health["drained_tick_evidence"] = [
                    {
                        "provider": tick.provider,
                        "pair": tick.pair,
                        "candle_interval": tick.candle_interval,
                        "price_field": tick.price_field,
                        "provider_timestamp": tick.provider_timestamp.isoformat(),
                        "received_at": tick.received_at.isoformat(),
                        "fresh": tick.fresh,
                        "sequence": tick.sequence,
                        "payload_hash": tick.payload_hash,
                        "source_version": tick.source_version,
                    }
                    for tick in ticks
                ]
                stream_health["binance_reference"] = reference_health
                assets = {
                    "BTC" if tick.pair == "BTCUSDT" else "ETH"
                    for tick in ticks
                    if tick.fresh
                }
                target_market_ids.extend(
                    market_id
                    for market_id, result in eligible.items()
                    if result.rule.asset in assets
                )
                if reference_health.get("status") not in {"connected", "disabled"}:
                    rest_fallback = True
            else:
                stream_health["binance_reference"] = {"status": "disabled"}

            if rest_fallback:
                target_market_ids.extend(eligible)
            target_market_ids = list(dict.fromkeys(target_market_ids))
            for market_id in target_market_ids:
                if analyzed >= self.analysis_limit:
                    break
                if market_id not in eligible:
                    continue
                try:
                    signal = self.workflow.analyze(market_id)
                except Exception as exc:
                    reasons.append(
                        f"{market_id}:analysis_error:{type(exc).__name__}"
                    )
                    continue
                analyzed += 1
                entry, created = self.paper.record(signal)
                if not created:
                    continue
                if entry.action == "enter":
                    entered += 1
                else:
                    skipped += 1
            if self.settlement is not None:
                try:
                    self.settlement.settle_due(limit=self.analysis_limit)
                except Exception as exc:
                    reasons.append(f"settlement_error:{type(exc).__name__}")
            self.paper.settle_open()
        except Exception as exc:
            reasons.append(f"cycle_error:{type(exc).__name__}")
        try:
            schema_report = self.schema_monitor.inspect_after(payload_boundary)
            stream_health["schema_drift"] = schema_report.as_dict()
            if schema_report.status == "drift_detected":
                reasons.extend(
                    f"schema_drift:{issue.contract}:{issue.code}"
                    for issue in schema_report.issues
                )
        except Exception as exc:
            stream_health["schema_drift"] = {
                "status": "monitor_error",
                "source_version": "external-payload-schema-monitor-v1",
                "error": type(exc).__name__,
            }
            reasons.append(f"schema_drift_monitor_error:{type(exc).__name__}")
        stream_health.setdefault("polymarket", {"status": "degraded"})
        stream_health.setdefault("binance_reference", {"status": "degraded"})

        completed_at = _utc(self.clock())
        status = (
            "degraded"
            if reasons
            else "complete_rest_fallback"
            if rest_fallback
            else "complete"
        )
        cycle = ShadowCycleResult(
            cycle_id=f"shadow:{uuid4()}",
            status=status,
            discovered_count=discovered,
            analyzed_count=analyzed,
            paper_entered_count=entered,
            paper_skipped_count=skipped,
            stream_health=stream_health,
            reasons=tuple(reasons),
            started_at=started_at,
            completed_at=completed_at,
            source_version=SHADOW_SOURCE_VERSION,
        )
        self.repository.save_shadow_cycle(cycle)
        return cycle


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
