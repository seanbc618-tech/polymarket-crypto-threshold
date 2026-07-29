"""Seven-asset 5m/15m Chainlink Up/Down research loop."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crypto_threshold.adapters.polymarket.base import MarketEventContext
from crypto_threshold.adapters.polymarket.translator import translate_market
from crypto_threshold.config import Settings
from crypto_threshold.domain.assets import ASSET_CONTRACTS, SHORT_UPDOWN_ASSETS
from crypto_threshold.domain.prices import Kline, KlineSeries
from crypto_threshold.domain.probability import SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION
from crypto_threshold.domain.rules import SHORT_UPDOWN_FAMILY, parse_contract
from crypto_threshold.services.cex_direction_service import (
    CEX_DIRECTION_FEATURE_NAMES,
    CEX_DIRECTION_MODEL_NAME,
    CexDirectionArtifact,
)
from crypto_threshold.services.discovery_service import DiscoveryService
from crypto_threshold.services.market_workflow_service import MarketWorkflowService
from crypto_threshold.services.replay_service import ReplayService
from crypto_threshold.services.schema_drift_service import ExternalPayloadSchemaMonitor
from crypto_threshold.services.settlement_service import (
    CHAINLINK_SETTLEMENT_SOURCE_VERSION,
    SettlementPendingError,
    SettlementService,
)
from crypto_threshold.services.shadow_monitor_service import ShadowMonitorService
from crypto_threshold.storage.db import Database
from crypto_threshold.storage.repositories import Repository

NOW = datetime(2026, 7, 25, 12, 2, tzinfo=UTC)


def _short_payload(
    asset: str,
    interval: str,
    *,
    market_id: str | None = None,
    start: datetime | None = None,
) -> dict[str, Any]:
    contract = ASSET_CONTRACTS[asset]
    minutes = 5 if interval == "5m" else 15
    window_start = start or (NOW - timedelta(minutes=2))
    end = window_start + timedelta(minutes=minutes)
    slug = f"{asset.lower()}-up-or-down-{interval}"
    event_id = f"event-{asset.lower()}-{interval}"
    identifier = market_id or f"market-{asset.lower()}-{interval}"
    description = (
        "This market will resolve to \"Up\" if the Chainlink "
        f"{contract.chainlink_pair} Data Stream value at the end of the time "
        "range specified in the title is greater than or equal to the value "
        "at the beginning of that range. Otherwise, this market will resolve "
        "to \"Down\"."
    )
    event = {
        "id": event_id,
        "startTime": window_start.isoformat(),
        "endDate": end.isoformat(),
        "seriesSlug": slug,
        "recurrence": "daily",
        "series": [{"slug": slug, "recurrence": "daily"}],
    }
    return {
        "id": identifier,
        "eventId": event_id,
        "conditionId": f"condition-{asset.lower()}-{interval}",
        "question": f"{contract.display_name} Up or Down?",
        "slug": f"{asset.lower()}-updown-window",
        "description": description,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "eventStartTime": window_start.isoformat(),
        "endDate": end.isoformat(),
        "outcomes": json.dumps(["Up", "Down"]),
        "clobTokenIds": json.dumps(
            [f"{identifier}-up-token", f"{identifier}-down-token"]
        ),
        "events": [event],
    }


def _window_price_payload(
    *,
    open_price: str | None = "99999.5",
    close_price: str | None = None,
    completed: bool = False,
) -> dict[str, Any]:
    return {
        "openPrice": open_price,
        "closePrice": close_price,
        "completed": completed,
        "incomplete": not completed,
        "cached": True,
        "timestamp": int(NOW.timestamp() * 1000),
    }


@pytest.mark.parametrize("asset", sorted(SHORT_UPDOWN_ASSETS))
@pytest.mark.parametrize("interval", ("5m", "15m"))
def test_parser_supports_all_fourteen_live_contract_shapes(
    asset: str,
    interval: str,
) -> None:
    market = translate_market(_short_payload(asset, interval), received_at=NOW)
    rule = parse_contract(market, now=NOW)

    assert rule.tradable
    assert rule.contract_family == SHORT_UPDOWN_FAMILY
    assert rule.asset == asset
    assert rule.settlement_provider == "chainlink"
    assert rule.pair == ASSET_CONTRACTS[asset].chainlink_pair
    assert rule.candle_interval == interval
    assert rule.price_field == "data_stream_value"
    assert rule.exact_operator == ">="
    assert rule.affirmative_outcome == "Up"
    assert rule.negative_outcome == "Down"
    assert rule.window_start_time_utc is not None
    assert rule.target_time_utc is not None


def test_short_parser_rejects_source_pair_duration_and_status_mismatch() -> None:
    payload = _short_payload("BTC", "5m")
    bad_source = {
        **payload,
        "description": str(payload["description"]).replace("Chainlink", "Binance"),
    }
    assert "unsupported_settlement_provider:binance" in parse_contract(
        translate_market(bad_source, received_at=NOW),
        now=NOW,
    ).rejection_reasons

    bad_pair = {
        **payload,
        "description": str(payload["description"]).replace("BTC/USD", "ETH/USD"),
    }
    assert any(
        reason.startswith("pair_mismatch:")
        for reason in parse_contract(
            translate_market(bad_pair, received_at=NOW),
            now=NOW,
        ).rejection_reasons
    )

    bad_duration = {
        **payload,
        "endDate": (NOW + timedelta(minutes=8)).isoformat(),
    }
    assert "window_duration_mismatch" in parse_contract(
        translate_market(bad_duration, received_at=NOW),
        now=NOW,
    ).rejection_reasons

    expired = _short_payload(
        "BTC",
        "5m",
        start=NOW - timedelta(minutes=10),
    )
    expired_rule = parse_contract(
        translate_market(expired, received_at=NOW),
        now=NOW,
    )
    assert "target_time_not_future" in expired_rule.rejection_reasons
    assert "gamma_market_expired" in expired_rule.rejection_reasons


class _ShortClient:
    def __init__(
        self,
        payloads: list[dict[str, Any]],
        *,
        window_price_payload: dict[str, Any] | None = None,
    ) -> None:
        self.payloads = payloads
        self.reads: list[str] = []
        self.resolution_events: dict[str, dict[str, Any]] = {}
        self.window_price_payload = (
            window_price_payload
            if window_price_payload is not None
            else _window_price_payload()
        )

    def discover_updown_markets(
        self,
        intervals: tuple[str, ...],
        *,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.reads.append(f"discover:{','.join(intervals)}")
        return self.payloads[:limit]

    def discover_markets(
        self,
        asset: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        raise AssertionError("daily discovery must not be used")

    def get_market(self, market_id: str) -> dict[str, Any]:
        self.reads.append("market")
        return next(payload for payload in self.payloads if payload["id"] == market_id)

    def get_event(self, event_id: str) -> dict[str, Any]:
        self.reads.append("event")
        return self.resolution_events[event_id]

    def get_crypto_window_price(
        self,
        asset: str,
        *,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        self.reads.append(f"crypto_price:{asset}:{interval}")
        return dict(self.window_price_payload)

    def get_market_event_context(
        self,
        market_id: str,
        condition_id: str | None,
        question: str,
    ) -> MarketEventContext:
        raise AssertionError("embedded event identity should be sufficient")

    def get_order_book(self, token_id: str) -> dict[str, Any]:
        self.reads.append(f"book:{token_id}")
        is_up = token_id.endswith("up-token")
        return {
            "timestamp": str(int(NOW.timestamp() * 1000)),
            "bids": [{"price": "0.48" if is_up else "0.47", "size": "100"}],
            "asks": [
                {"price": "0.50" if is_up else "0.51", "size": "100"}
            ],
        }

    def get_market_info(self, condition_id: str) -> dict[str, Any]:
        self.reads.append("market_info")
        return {"fd": {"r": 0.07, "e": 1, "to": True}}


class _NoDailyProvider:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"daily provider must not be used: {name}")


class _CexKlineProvider:
    def __init__(self, *, checkpoint: datetime) -> None:
        start = checkpoint - timedelta(minutes=31)
        self.series = KlineSeries(
            asset="BTC",
            quote="USDT",
            provider="binance",
            symbol="BTCUSDT",
            interval="1m",
            klines=tuple(
                Kline(
                    open_time=start + timedelta(minutes=index),
                    close_time=start
                    + timedelta(minutes=index + 1)
                    - timedelta(milliseconds=1),
                    open=Decimal("100000") + Decimal(index * 10),
                    high=Decimal("100015") + Decimal(index * 10),
                    low=Decimal("99995") + Decimal(index * 10),
                    close=Decimal("100010") + Decimal(index * 10),
                    volume=Decimal("100") + Decimal(index),
                )
                for index in range(31)
            ),
            received_at=checkpoint + timedelta(seconds=1),
            source_version="binance-test-v1",
            raw_payload=[
                [
                    int((checkpoint - timedelta(minutes=1)).timestamp() * 1000),
                    "100000",
                    "100020",
                    "99990",
                    "100010",
                    "100",
                    int((checkpoint - timedelta(milliseconds=1)).timestamp() * 1000),
                ]
            ],
        )

    def get_klines(self, *args: Any, **kwargs: Any) -> KlineSeries:
        return self.series


def _write_test_artifact(path: Path) -> None:
    provisional = CexDirectionArtifact(
        decision_lead_seconds=60,
        means=(0.0,) * len(CEX_DIRECTION_FEATURE_NAMES),
        scales=(1.0,) * len(CEX_DIRECTION_FEATURE_NAMES),
        weights=(0.0,) * len(CEX_DIRECTION_FEATURE_NAMES),
        intercept=math.log(4),
        probability_margin=0.03,
        training={
            "training_cutoff_time_utc": (
                NOW - timedelta(days=1)
            ).isoformat(),
            "sample_count": 1000,
        },
        artifact_hash="pending",
    )
    path.write_text(json.dumps(provisional.as_payload()), encoding="utf-8")


def _short_workflow(
    tmp_path: Path,
    *,
    window_price_payload: dict[str, Any] | None = None,
) -> tuple[MarketWorkflowService, Repository, _ShortClient]:
    database = Database(tmp_path / "short.db")
    database.initialize()
    repository = Repository(database)
    payload = _short_payload(
        "BTC",
        "5m",
        start=NOW - timedelta(minutes=4),
    )
    client = _ShortClient(
        [payload],
        window_price_payload=window_price_payload,
    )
    model_path = tmp_path / "cex-model.json"
    _write_test_artifact(model_path)
    settings = Settings(
        DATABASE_PATH=str(database.path),
        SHORT_CEX_MODEL_PATH=str(model_path),
        _env_file=None,
    )
    workflow = MarketWorkflowService(
        client=client,
        repository=repository,
        binance=_CexKlineProvider(checkpoint=NOW),  # type: ignore[arg-type]
        coinbase=_NoDailyProvider(),  # type: ignore[arg-type]
        settings=settings,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    return workflow, repository, client


def test_workflow_predicts_from_closed_cex_klines_without_boundary_input(
    tmp_path: Path,
) -> None:
    workflow, repository, client = _short_workflow(tmp_path)

    signal = workflow.analyze("market-btc-5m")

    assert signal.status == "analyzed"
    assert signal.contract_family == SHORT_UPDOWN_FAMILY
    assert signal.affirmative_outcome == "Up"
    assert signal.negative_outcome == "Down"
    assert signal.threshold is None
    assert signal.model_name == CEX_DIRECTION_MODEL_NAME
    assert signal.estimated_probability == Decimal("0.8")
    assert signal.probability_low == Decimal("0.77")
    assert signal.selected_outcome == "YES"
    assert signal.source_version == SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION
    roles = {
        str(row["input_role"])
        for row in repository.signal_input_rows(signal.signal_id)
    }
    assert {
        "market",
        "up_book",
        "down_book",
        "market_info_fee_schedule",
        "cex_direction_klines_1m",
        "cex_direction_model",
    }.issubset(roles)
    assert "authoritative_window_price" not in roles
    assert "chainlink_current_price" not in roles
    assert not any(read.startswith("crypto_price:") for read in client.reads)
    report = ExternalPayloadSchemaMonitor(repository).inspect_after(0)
    assert report.status == "ok"


def test_workflow_fails_closed_when_sealed_cex_model_is_missing(
    tmp_path: Path,
) -> None:
    workflow, repository, client = _short_workflow(tmp_path)
    workflow.settings.SHORT_CEX_MODEL_PATH = str(tmp_path / "missing-model.json")
    workflow._cex_direction_artifact = None

    signal = workflow.analyze("market-btc-5m")

    assert signal.status == "rejected"
    assert signal.threshold is None
    assert "cex_direction_model_unavailable:FileNotFoundError" in signal.reasons
    roles = {
        str(row["input_role"])
        for row in repository.signal_input_rows(signal.signal_id)
    }
    assert roles == {"market"}
    assert not any(read.startswith("book:") for read in client.reads)
    assert repository.table_count("analysis_signals") == 1


def test_workflow_waits_until_the_sealed_cex_checkpoint(
    tmp_path: Path,
) -> None:
    workflow, _, client = _short_workflow(tmp_path)
    workflow.clock = lambda: NOW - timedelta(seconds=1)

    signal = workflow.analyze("market-btc-5m")

    assert signal.status == "rejected"
    assert "cex_direction_checkpoint_not_reached" in signal.reasons
    assert not any(read.startswith("book:") for read in client.reads)


def test_workflow_never_reads_provisional_chainlink_open_for_prediction(
    tmp_path: Path,
) -> None:
    workflow, _, client = _short_workflow(
        tmp_path,
        window_price_payload=_window_price_payload(
            open_price="1",
            close_price=None,
            completed=False,
        ),
    )

    signal = workflow.analyze("market-btc-5m")

    assert signal.status == "analyzed"
    assert signal.threshold is None
    assert not any(read.startswith("crypto_price:") for read in client.reads)


def test_discovery_persists_exactly_fourteen_open_markets_without_duplicates(
    tmp_path: Path,
) -> None:
    payloads = [
        _short_payload(asset, interval)
        for interval in ("5m", "15m")
        for asset in sorted(SHORT_UPDOWN_ASSETS)
    ]
    client = _ShortClient(payloads + payloads)
    database = Database(tmp_path / "discovery.db")
    database.initialize()
    repository = Repository(database)
    service = DiscoveryService(client, repository, clock=lambda: NOW)

    first = service.discover_updown(limit=50)
    second = service.discover_updown(limit=50)

    assert len(first) == 14
    assert len(second) == 14
    assert all(result.rule.tradable for result in first)
    assert repository.table_count("markets") == 14
    assert repository.table_count("resolution_rules") == 14


def test_chainlink_settlement_tie_resolves_up_and_persists_raw_first(
    tmp_path: Path,
) -> None:
    workflow, repository, client = _short_workflow(tmp_path)
    signal = workflow.analyze("market-btc-5m")
    boundary = Decimal("99999.5")
    assert signal.status == "analyzed"
    assert signal.threshold is None and signal.deadline is not None
    client.window_price_payload = _window_price_payload(
        open_price=str(boundary),
        close_price=str(boundary),
        completed=True,
    )
    event_id = "event-btc-5m"
    client.resolution_events[event_id] = {
        "id": event_id,
        "eventMetadata": {
            "priceToBeat": str(boundary),
            "finalPrice": str(boundary),
        },
        "markets": [
            {
                "id": signal.market_id,
                "conditionId": "condition-btc-5m",
                "closed": True,
                "outcomes": json.dumps(["Up", "Down"]),
                "outcomePrices": json.dumps(["1", "0"]),
            }
        ],
    }
    service = SettlementService(
        repository=repository,
        binance=_NoDailyProvider(),  # type: ignore[arg-type]
        client=client,
        clock=lambda: signal.deadline + timedelta(minutes=1),
    )

    label = service.settle_market(signal.market_id)

    assert label.outcome_yes
    assert label.provider == "chainlink"
    assert label.strike == label.observed_value == boundary
    assert label.source_version == CHAINLINK_SETTLEMENT_SOURCE_VERSION
    assert label.contract_family == SHORT_UPDOWN_FAMILY
    payloads = repository.external_payload_rows_after(label.payload_id - 1)
    assert len(payloads) == 1
    assert payloads[0]["payload_kind"] == "authoritative_window_price"
    replay = ReplayService(
        repository,
        clock=lambda: signal.deadline + timedelta(minutes=2),
    ).build(
        "short-updown-v1",
        contract_family=SHORT_UPDOWN_FAMILY,
    )
    assert replay.item_count == 1
    assert ReplayService(repository).verify(replay.dataset_id).ok


def test_chainlink_settlement_waits_for_final_price(tmp_path: Path) -> None:
    workflow, repository, client = _short_workflow(tmp_path)
    signal = workflow.analyze("market-btc-5m")
    boundary = Decimal("99999.5")
    assert signal.threshold is None and signal.deadline is not None
    client.resolution_events["event-btc-5m"] = {
        "id": "event-btc-5m",
        "eventMetadata": {"priceToBeat": str(boundary)},
        "markets": [
            {
                "id": signal.market_id,
                "closed": True,
                "outcomes": ["Up", "Down"],
                "outcomePrices": ["0.5", "0.5"],
            }
        ],
    }
    service = SettlementService(
        repository=repository,
        binance=_NoDailyProvider(),  # type: ignore[arg-type]
        client=client,
        clock=lambda: signal.deadline + timedelta(minutes=1),
    )

    with pytest.raises(SettlementPendingError):
        service.settle_market(signal.market_id)
    assert repository.get_settlement_label(signal.market_id) is None


def test_settlement_rejects_material_gamma_boundary_disagreement(
    tmp_path: Path,
) -> None:
    workflow, repository, client = _short_workflow(tmp_path)
    signal = workflow.analyze("market-btc-5m")
    endpoint_start = Decimal("99999.5")
    assert signal.threshold is None and signal.deadline is not None
    authoritative_start = endpoint_start + Decimal("1")
    client.window_price_payload = _window_price_payload(
        open_price=str(endpoint_start),
        close_price=str(endpoint_start + Decimal("1")),
        completed=True,
    )
    client.resolution_events["event-btc-5m"] = {
        "id": "event-btc-5m",
        "eventMetadata": {
            "priceToBeat": str(authoritative_start),
            "finalPrice": str(authoritative_start + Decimal("1")),
        },
        "markets": [
            {
                "id": signal.market_id,
                "closed": True,
                "outcomes": ["Up", "Down"],
                "outcomePrices": ["1", "0"],
            }
        ],
    }
    with pytest.raises(ValueError, match="official boundary sources disagree"):
        SettlementService(
            repository=repository,
            binance=_NoDailyProvider(),  # type: ignore[arg-type]
            client=client,
            clock=lambda: signal.deadline + timedelta(minutes=1),
        ).settle_market(signal.market_id)
    assert repository.get_settlement_label(signal.market_id) is None


def test_settlement_accepts_single_ulp_json_difference_and_replay_stays_exact(
    tmp_path: Path,
) -> None:
    endpoint_open = "1876.9833419425354"
    gamma_open = "1876.9833419425356"
    workflow, repository, client = _short_workflow(
        tmp_path,
        window_price_payload=_window_price_payload(open_price=endpoint_open),
    )
    signal = workflow.analyze("market-btc-5m")
    assert signal.threshold is None
    assert signal.deadline is not None
    endpoint_close = "1877"
    client.window_price_payload = _window_price_payload(
        open_price=endpoint_open,
        close_price=endpoint_close,
        completed=True,
    )
    client.resolution_events["event-btc-5m"] = {
        "id": "event-btc-5m",
        "eventMetadata": {
            "priceToBeat": gamma_open,
            "finalPrice": endpoint_close,
        },
        "markets": [
            {
                "id": signal.market_id,
                "conditionId": "condition-btc-5m",
                "closed": True,
                "outcomes": ["Up", "Down"],
                "outcomePrices": ["1", "0"],
            }
        ],
    }

    label = SettlementService(
        repository=repository,
        binance=_NoDailyProvider(),  # type: ignore[arg-type]
        client=client,
        clock=lambda: signal.deadline + timedelta(minutes=1),
    ).settle_market(signal.market_id)

    assert label.strike == Decimal(endpoint_open)
    replay = ReplayService(repository).build(
        "single-ulp-boundary",
        contract_family=SHORT_UPDOWN_FAMILY,
    )
    assert replay.item_count == 1
    assert ReplayService(repository).verify(replay.dataset_id).ok

def test_short_shadow_analyzes_only_due_strategy_markets_in_one_cycle(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "shadow.db")
    database.initialize()
    repository = Repository(database)
    results = [
        SimpleNamespace(
            market=SimpleNamespace(market_id=f"market-{index}"),
            rule=SimpleNamespace(tradable=True, asset=asset),
        )
        for index, asset in enumerate(
            sorted(SHORT_UPDOWN_ASSETS) * 2,
            start=1,
        )
    ]

    class Discovery:
        def discover_updown(self, *, limit: int) -> list[Any]:
            return results[:limit]

    class Workflow:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def short_signal_due(self, rule: Any, *, at: datetime) -> bool:
            return True

        def analyze(self, market_id: str) -> Any:
            self.calls.append(market_id)
            return SimpleNamespace()

    class Paper:
        def record(self, signal: Any) -> tuple[Any, bool]:
            return SimpleNamespace(action="skip"), True

        def settle_open(self) -> int:
            return 0

    workflow = Workflow()
    monitor = ShadowMonitorService(
        repository=repository,
        discovery=Discovery(),  # type: ignore[arg-type]
        workflow=workflow,  # type: ignore[arg-type]
        paper=Paper(),  # type: ignore[arg-type]
        contract_family=SHORT_UPDOWN_FAMILY,
        discovery_limit=14,
        analysis_limit=14,
        clock=lambda: NOW,
    )

    cycle = monitor.run_once()

    assert len(workflow.calls) == 14
    assert cycle.discovered_count == 14
    assert cycle.analyzed_count == 14
    assert cycle.contract_family == SHORT_UPDOWN_FAMILY
    row = repository.list_shadow_cycles(limit=1)[0]
    assert row["contract_family"] == SHORT_UPDOWN_FAMILY
