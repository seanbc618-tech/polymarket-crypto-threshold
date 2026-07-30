"""Independent public-data R1/R2/R3 shadow collection and gate orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import fmean
from typing import Any
from uuid import uuid4

from crypto_threshold.adapters.prices.binance_microstructure import (
    BinanceMicrostructureRestClient,
    BinanceMicrostructureStream,
)
from crypto_threshold.domain.factor_research import (
    FactorComparator,
    FactorRule,
    FactorTradeSide,
)
from crypto_threshold.domain.microstructure import L2MicrostructureFeatures
from crypto_threshold.domain.microstructure_capture import (
    MicrostructureCycleResult,
    MicrostructureFeatureSample,
)
from crypto_threshold.domain.research_integrity import (
    FeatureVector,
    ResearchRow,
    ResearchSource,
)
from crypto_threshold.services.binance_tape_service import (
    BinanceTapeError,
    BinanceTapeService,
    BuiltBinanceTape,
)
from crypto_threshold.services.factor_screening_service import (
    FactorScreeningService,
)
from crypto_threshold.services.hft_replay_service import HftReplayService
from crypto_threshold.services.research_integrity_service import (
    RESEARCH_INTEGRITY_SOURCE_VERSION,
    ResearchIntegrityError,
    ResearchIntegrityService,
)
from crypto_threshold.storage.microstructure_store import MicrostructureStore

MICROSTRUCTURE_SHADOW_SOURCE_VERSION = "microstructure-shadow-r1-r3-v1"
MICROSTRUCTURE_FACTOR_VERSION = "microstructure-causal-factors-v1"


@dataclass(frozen=True)
class MicrostructureShadowConfig:
    symbols: tuple[str, ...]
    poll_seconds: float
    snapshot_seconds: float
    feature_seconds: float
    integrity_seconds: float
    depth_levels: int
    trade_lookback_seconds: float
    event_batch_limit: int
    integrity_sample_limit: int
    warmup_seconds: float = 2.0


class MicrostructureShadowService:
    """Collect raw public data and run research gates without a trading surface."""

    def __init__(
        self,
        *,
        store: MicrostructureStore,
        stream: BinanceMicrostructureStream,
        rest: BinanceMicrostructureRestClient,
        config: MicrostructureShadowConfig,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if (
            not config.symbols
            or config.poll_seconds <= 0
            or config.snapshot_seconds <= 0
            or config.feature_seconds <= 0
            or config.integrity_seconds <= 0
            or config.depth_levels < 1
            or config.trade_lookback_seconds <= 0
            or config.event_batch_limit < 1
            or config.integrity_sample_limit < 102
            or config.warmup_seconds < 0
        ):
            raise ValueError("invalid microstructure shadow configuration")
        self.store = store
        self.stream = stream
        self.rest = rest
        self.config = config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper or time.sleep
        self.monotonic = monotonic or time.monotonic
        self._last_snapshot = float("-inf")
        self._last_feature = float("-inf")
        self._last_integrity = float("-inf")
        self._last_dropped = 0
        self._tapes = BinanceTapeService()
        self._replay = HftReplayService()
        self._integrity = ResearchIntegrityService()
        self._factor = FactorScreeningService()

    def run(
        self,
        *,
        duration_hours: float | None = None,
        once: bool = False,
    ) -> str:
        if once and duration_hours is not None:
            raise ValueError("once and duration_hours are mutually exclusive")
        if duration_hours is not None and duration_hours <= 0:
            raise ValueError("duration_hours must be positive")
        started_at = _utc(self.clock())
        session_id = f"micro:{uuid4()}"
        config_hash = _hash(asdict(self.config))
        self.store.start_session(
            session_id=session_id,
            symbols=self.config.symbols,
            config_hash=config_hash,
            started_at=started_at,
            source_version=MICROSTRUCTURE_SHADOW_SOURCE_VERSION,
        )
        self._predeclare_factor_screen(session_id, started_at=started_at)
        started_monotonic = self.monotonic()
        reasons: list[str] = []
        status = "complete"
        self.stream.start()
        try:
            if self.config.warmup_seconds:
                self.sleeper(self.config.warmup_seconds)
            while True:
                cycle = self.cycle(session_id)
                reasons.extend(cycle.reasons)
                if once:
                    break
                if (
                    duration_hours is not None
                    and self.monotonic() - started_monotonic
                    >= duration_hours * 3600
                ):
                    break
                self.sleeper(self.config.poll_seconds)
        except KeyboardInterrupt:
            status = "interrupted"
            reasons.append("operator_interrupt")
        except Exception as exc:
            status = "failed"
            reasons.append(f"fatal:{type(exc).__name__}:{exc}")
            raise
        finally:
            self.stream.stop()
            trailing = self.stream.drain(limit=self.config.event_batch_limit)
            if trailing:
                self.store.save_events(session_id, trailing)
            self.rest.close()
            if status == "complete" and reasons:
                status = "complete_with_rejections"
            self.store.finish_session(
                session_id,
                status=status,
                reasons=tuple(dict.fromkeys(reasons)),
                completed_at=_utc(self.clock()),
            )
        return session_id

    def cycle(self, session_id: str) -> MicrostructureCycleResult:
        started_at = _utc(self.clock())
        now_monotonic = self.monotonic()
        reasons: list[str] = []
        snapshots = 0
        marks = 0
        features = 0
        integrity_runs = 0
        factor_runs = 0
        health = self.stream.health()
        detail = health.get("detail")
        dropped = (
            int(detail.get("dropped", 0))
            if isinstance(detail, dict)
            else 0
        )
        snapshot_due = (
            now_monotonic - self._last_snapshot >= self.config.snapshot_seconds
            or dropped > self._last_dropped
        )
        if dropped > self._last_dropped:
            reasons.append(
                f"stream_overflow_resnapshot:{dropped - self._last_dropped}"
            )
        self._last_dropped = dropped

        if snapshot_due:
            for symbol in self.config.symbols:
                try:
                    snapshot = self.rest.depth_snapshot(symbol)
                    snapshots += len(self.store.save_events(session_id, (snapshot,)))
                except Exception as exc:
                    reasons.append(
                        f"{symbol}:snapshot:{type(exc).__name__}:{exc}"
                    )
                try:
                    mark = self.rest.perpetual_mark(symbol)
                    marks += len(
                        self.store.save_perpetual_marks(session_id, (mark,))
                    )
                except Exception as exc:
                    reasons.append(f"{symbol}:mark:{type(exc).__name__}:{exc}")
            self._last_snapshot = now_monotonic

        raw_events = self.stream.drain(limit=self.config.event_batch_limit)
        event_ids = self.store.save_events(session_id, raw_events) if raw_events else ()

        if now_monotonic - self._last_feature >= self.config.feature_seconds:
            feature_values: dict[
                str, tuple[BuiltBinanceTape, L2MicrostructureFeatures]
            ] = {}
            for symbol in self.config.symbols:
                try:
                    rows = self.store.latest_tape_rows(
                        session_id=session_id,
                        symbol=symbol,
                    )
                    tape = self._tapes.build(rows)
                    extracted = self._replay.extract_features(
                        tape.events,
                        as_of_event_id=tape.events[-1].event_id,
                        depth_levels=self.config.depth_levels,
                        trade_lookback=timedelta(
                            seconds=self.config.trade_lookback_seconds
                        ),
                    )
                    feature_values[symbol] = (tape, extracted)
                except (BinanceTapeError, ValueError) as exc:
                    reasons.append(f"{symbol}:feature:{type(exc).__name__}:{exc}")
            recent = self.store.recent_feature_rows(
                session_id=session_id,
                limit=max(self.config.integrity_sample_limit * 4, 500),
            )
            current_midpoints = {
                symbol: (value[1].as_of_exchange_at, value[1].midpoint)
                for symbol, value in feature_values.items()
            }
            for symbol, (tape_value, extracted_value) in feature_values.items():
                tape = tape_value
                extracted = extracted_value
                mark_row = self.store.latest_mark_row(
                    session_id=session_id,
                    symbol=symbol,
                )
                basis = _basis_bps(mark_row, extracted.midpoint)
                lead = _btc_lead_correlation(
                    recent,
                    symbol=symbol,
                    current_midpoints=current_midpoints,
                    bucket_seconds=self.config.feature_seconds,
                )
                sample = MicrostructureFeatureSample(
                    sample_id=f"feature:{uuid4()}",
                    session_id=session_id,
                    symbol=symbol,
                    as_of_exchange_at=extracted.as_of_exchange_at,
                    as_of_received_at=extracted.as_of_received_at,
                    best_bid=extracted.best_bid,
                    best_ask=extracted.best_ask,
                    midpoint=extracted.midpoint,
                    spread=extracted.spread,
                    bid_depth=extracted.bid_depth,
                    ask_depth=extracted.ask_depth,
                    book_imbalance=extracted.book_imbalance,
                    microprice=extracted.microprice,
                    vamp=extracted.vamp,
                    aggressive_trade_imbalance=(
                        extracted.aggressive_trade_imbalance
                    ),
                    feed_latency_ms=extracted.feed_latency_ms,
                    spot_perpetual_basis_bps=basis,
                    btc_lead_correlation=lead,
                    source_event_ids=tape.raw_event_ids,
                    source_payload_hashes=(_tape_hash(tape.payload_hashes),),
                )
                features += int(self.store.save_feature_sample(sample))
            self._last_feature = now_monotonic

        if now_monotonic - self._last_integrity >= self.config.integrity_seconds:
            integrity_runs += self._run_integrity(session_id, reasons=reasons)
            self._last_integrity = now_monotonic

        status = "complete" if not reasons else "complete_with_rejections"
        return MicrostructureCycleResult(
            session_id=session_id,
            persisted_events=len(event_ids),
            persisted_snapshots=snapshots,
            persisted_marks=marks,
            feature_samples=features,
            integrity_runs=integrity_runs,
            factor_runs=factor_runs,
            status=status,
            reasons=tuple(dict.fromkeys(reasons)),
            started_at=started_at,
            completed_at=_utc(self.clock()),
        )

    def _run_integrity(self, session_id: str, *, reasons: list[str]) -> int:
        rows = self.store.recent_feature_rows(
            session_id=session_id,
            limit=self.config.integrity_sample_limit * len(self.config.symbols),
        )
        btc_rows = tuple(row for row in rows if str(row["symbol"]) == "BTCUSDT")
        if len(btc_rows) < 102:
            reasons.append(f"integrity_collecting:{len(btc_rows)}/102")
            return 0
        research_rows = _research_rows(
            btc_rows[-self.config.integrity_sample_limit :],
            feature_seconds=self.config.feature_seconds,
        )
        created_at = _utc(self.clock())
        try:
            report = self._integrity.analyze(
                research_rows,
                _causal_feature_builder,
                feature_builder_version=MICROSTRUCTURE_FACTOR_VERSION,
                startup_rows=(20, 50, 100),
                max_timestamp_gap=timedelta(
                    seconds=self.config.feature_seconds * 3
                ),
            )
            split = self._integrity.chronological_split(
                research_rows,
                train_fraction=0.7,
                purge=timedelta(minutes=15),
                embargo=timedelta(minutes=15),
            )
            status = "passed" if report.passed else "failed"
            payload: object = {"analysis": report, "split": split}
            manifest_hash = _hash(
                {
                    "analysis_manifest": report.manifest_hash,
                    "split_manifest": split.manifest_hash,
                }
            )
        except ResearchIntegrityError as exc:
            reasons.append(f"integrity_collecting:{exc}")
            return 0
        self.store.save_integrity_run(
            run_id=f"integrity:{uuid4()}",
            session_id=session_id,
            status=status,
            row_count=len(research_rows),
            manifest_hash=manifest_hash,
            report=payload,
            created_at=created_at,
        )
        return 1

    def _predeclare_factor_screen(
        self,
        session_id: str,
        *,
        started_at: datetime,
    ) -> None:
        rules = (
            FactorRule(
                rule_id="obi-positive-010",
                factor_name="book_imbalance",
                comparator=FactorComparator.GREATER_THAN,
                threshold=Decimal("0.10"),
                trade_side=FactorTradeSide.YES,
            ),
            FactorRule(
                rule_id="trade-positive-010",
                factor_name="aggressive_trade_imbalance",
                comparator=FactorComparator.GREATER_THAN,
                threshold=Decimal("0.10"),
                trade_side=FactorTradeSide.YES,
            ),
            FactorRule(
                rule_id="basis-negative-2bps",
                factor_name="spot_perpetual_basis_bps",
                comparator=FactorComparator.LESS_THAN,
                threshold=Decimal("-2"),
                trade_side=FactorTradeSide.NO,
            ),
            FactorRule(
                rule_id="btc-lead-positive-020",
                factor_name="btc_lead_correlation",
                comparator=FactorComparator.GREATER_THAN,
                threshold=Decimal("0.20"),
                trade_side=FactorTradeSide.YES,
            ),
        )
        spec = self._factor.seal_spec(
            experiment_id=f"microstructure-r3:{started_at.date().isoformat()}",
            spec_version="microstructure-factor-grid-v1",
            created_at=started_at,
            training_cutoff_at=started_at - timedelta(microseconds=1),
            minimum_oos_groups=20,
            minimum_dates=7,
            minimum_groups_per_asset=4,
            required_assets=("BTC", "ETH", "SOL", "XRP"),
            stake_usdc=Decimal("10"),
            frozen_model_version="cex-kline-chainlink-direction-v1-frozen",
            market_baseline_version="polymarket-executable-midpoint-v1",
            integrity_source_version=RESEARCH_INTEGRITY_SOURCE_VERSION,
            replay_source_version="hft-inspired-l2-replay-r1-v1",
            rules=rules,
        )
        isolation = self._integrity.dry_run_isolation(
            trading_disabled=True,
            credentials_present=False,
            authenticated_channel_enabled=False,
            mutation_surface_enabled=False,
        )
        if not isolation.passed:
            raise RuntimeError("microstructure dry-run isolation gate failed")
        self.store.save_factor_run(
            run_id=f"factor-plan:{uuid4()}",
            session_id=session_id,
            experiment_id=spec.experiment_id,
            status="preregistered_waiting_for_settled_oos",
            spec_hash=spec.spec_hash,
            report={
                "spec": spec,
                "dry_run_isolation": isolation,
                "trials": [],
                "promotion_allowed": False,
            },
            created_at=started_at,
        )


def _research_rows(
    rows: tuple[Any, ...],
    *,
    feature_seconds: float,
) -> tuple[ResearchRow, ...]:
    result: list[ResearchRow] = []
    for row in rows:
        decision = _time(row["as_of_received_at"])
        observed = _time(row["as_of_exchange_at"])
        payload_hashes = json.loads(str(row["source_payload_hashes_json"]))
        if not isinstance(payload_hashes, list) or not payload_hashes:
            raise ResearchIntegrityError("feature_sample_source_hashes_missing")
        source = ResearchSource(
            source_id=f"{row['sample_id']}:tape",
            role="binance_l2_tape",
            observed_at=observed,
            received_at=decision,
            content_hash=str(payload_hashes[0]),
        )
        target = decision + timedelta(minutes=5)
        result.append(
            ResearchRow(
                row_id=str(row["sample_id"]),
                event_group_id=f"{row['symbol']}:{target.isoformat()}",
                asset=str(row["symbol"]).removesuffix("USDT"),
                target_time_utc=target,
                feature_window_start=decision
                - timedelta(seconds=feature_seconds * 10),
                decision_at=decision,
                label_available_at=target + timedelta(minutes=1),
                inputs={
                    "midpoint": float(row["midpoint"]),
                    "book_imbalance": float(row["book_imbalance"]),
                    "microprice": float(row["microprice"]),
                    "vamp": float(row["vamp"]),
                    "aggressive_trade_imbalance": float(
                        row["aggressive_trade_imbalance"]
                    ),
                    "spot_perpetual_basis_bps": (
                        float(row["spot_perpetual_basis_bps"])
                        if row["spot_perpetual_basis_bps"] is not None
                        else None
                    ),
                    "btc_lead_correlation": (
                        float(row["btc_lead_correlation"])
                        if row["btc_lead_correlation"] is not None
                        else None
                    ),
                },
                sources=(source,),
            )
        )
    return tuple(result)


def _causal_feature_builder(
    rows: tuple[ResearchRow, ...],
) -> tuple[FeatureVector, ...]:
    history: dict[str, list[ResearchRow]] = defaultdict(list)
    result: list[FeatureVector] = []
    for row in rows:
        asset_history = history[row.asset]
        asset_history.append(row)
        obi = [
            float(item.inputs["book_imbalance"] or 0)
            for item in asset_history[-10:]
        ]
        trade = [
            float(item.inputs["aggressive_trade_imbalance"] or 0)
            for item in asset_history[-10:]
        ]
        midpoint = float(row.inputs["midpoint"] or 0)
        previous_midpoint = (
            float(asset_history[-2].inputs["midpoint"] or 0)
            if len(asset_history) > 1
            else midpoint
        )
        result.append(
            FeatureVector(
                row_id=row.row_id,
                values={
                    **row.inputs,
                    "obi_mean_10": fmean(obi),
                    "trade_mean_10": fmean(trade),
                    "midpoint_return_1": (
                        midpoint / previous_midpoint - 1
                        if previous_midpoint > 0
                        else 0.0
                    ),
                },
            )
        )
    return tuple(result)


def _basis_bps(row: Any | None, spot_midpoint: Decimal) -> Decimal | None:
    if row is None or spot_midpoint <= 0:
        return None
    normalized = json.loads(str(row["normalized_json"]))
    if not isinstance(normalized, dict) or normalized.get("price") is None:
        return None
    mark = Decimal(str(normalized["price"]))
    return (mark - spot_midpoint) / spot_midpoint * Decimal("10000")


def _btc_lead_correlation(
    rows: tuple[Any, ...],
    *,
    symbol: str,
    current_midpoints: dict[str, tuple[datetime, Decimal]],
    bucket_seconds: float,
) -> Decimal | None:
    if symbol == "BTCUSDT" or bucket_seconds <= 0:
        return None
    by_symbol: dict[str, dict[int, float]] = defaultdict(dict)
    for row in rows:
        row_symbol = str(row["symbol"])
        bucket = int(_time(row["as_of_exchange_at"]).timestamp() // bucket_seconds)
        by_symbol[row_symbol][bucket] = float(row["midpoint"])
    for row_symbol, (observed_at, midpoint) in current_midpoints.items():
        bucket = int(observed_at.timestamp() // bucket_seconds)
        by_symbol[row_symbol][bucket] = float(midpoint)
    leader_returns = _returns(by_symbol.get("BTCUSDT", {}))
    follower_returns = _returns(by_symbol.get(symbol, {}))
    pairs = [
        (leader_returns[bucket - 1], follower_return)
        for bucket, follower_return in sorted(follower_returns.items())
        if bucket - 1 in leader_returns
    ]
    if len(pairs) < 8:
        return None
    left = [pair[0] for pair in pairs]
    right = [pair[1] for pair in pairs]
    correlation = _pearson(left, right)
    return Decimal(str(correlation)) if correlation is not None else None


def _returns(points: dict[int, float]) -> dict[int, float]:
    result: dict[int, float] = {}
    previous_bucket: int | None = None
    previous_value: float | None = None
    for bucket, value in sorted(points.items()):
        if (
            previous_bucket is not None
            and previous_value is not None
            and bucket == previous_bucket + 1
            and previous_value > 0
        ):
            result[bucket] = math.log(value / previous_value)
        previous_bucket = bucket
        previous_value = value
    return result


def _pearson(left: list[float], right: list[float]) -> float | None:
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left, right, strict=True)
    )
    left_scale = sum((value - left_mean) ** 2 for value in left)
    right_scale = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_scale * right_scale)
    return numerator / denominator if denominator > 0 else None


def _tape_hash(payload_hashes: tuple[str, ...]) -> str:
    return hashlib.sha256(
        "\n".join(payload_hashes).encode("utf-8")
    ).hexdigest()


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
