"""R0 multi-checkpoint market baseline and public-book latency replay."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from sqlite3 import Row
from uuid import uuid4

from crypto_threshold.adapters.polymarket.base import PolymarketReadClient
from crypto_threshold.adapters.polymarket.translator import (
    CLOB_BOOK_SOURCE_VERSION,
    translate_order_book,
)
from crypto_threshold.domain.fees import compute_taker_fee
from crypto_threshold.domain.markets import AskExecution, OrderBookSnapshot, calculate_ask_vwap
from crypto_threshold.domain.probability import AnalysisSignal
from crypto_threshold.domain.research import (
    ShortChallengerObservation,
    ShortLatencyReplay,
)
from crypto_threshold.domain.rules import SHORT_UPDOWN_FAMILY, CryptoResolutionRule
from crypto_threshold.storage.repositories import Repository

SHORT_CHALLENGER_SOURCE_VERSION = "short-challenger-r0-v1"
SHORT_LATENCY_SOURCE_VERSION = "short-latency-replay-r0-v1"


@dataclass(frozen=True)
class ShortChallengerCaptureResult:
    observation_id: str
    observation_created: bool
    replay_created_count: int
    paper_entered_count: int


class ShortChallengerService:
    """Collect isolated R0 evidence without authenticated exchange operations."""

    def __init__(
        self,
        repository: Repository,
        client: PolymarketReadClient,
        *,
        min_net_ev: Decimal,
        checkpoints_seconds: tuple[int, ...],
        latencies_ms: tuple[int, ...],
        max_book_age_seconds: int,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if not checkpoints_seconds or any(
            checkpoint < 30 or checkpoint > 300
            for checkpoint in checkpoints_seconds
        ):
            raise ValueError("short challenger checkpoints are invalid")
        if (
            not latencies_ms
            or latencies_ms[0] != 0
            or tuple(sorted(set(latencies_ms))) != latencies_ms
            or any(latency < 0 for latency in latencies_ms)
        ):
            raise ValueError("short challenger latencies are invalid")
        if min_net_ev < 0:
            raise ValueError("short challenger net-EV threshold must be non-negative")
        if max_book_age_seconds <= 0:
            raise ValueError("short challenger book age must be positive")
        self.repository = repository
        self.client = client
        self.min_net_ev = min_net_ev
        self.checkpoints_seconds = checkpoints_seconds
        self.latencies_ms = latencies_ms
        self.max_book_age_seconds = max_book_age_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper or time.sleep
        self.monotonic = monotonic or time.monotonic

    def record(
        self,
        signal: AnalysisSignal,
        rule: CryptoResolutionRule,
        *,
        checkpoint_lead_seconds: int,
    ) -> ShortChallengerCaptureResult:
        if signal.contract_family != SHORT_UPDOWN_FAMILY:
            raise ValueError("short challenger requires a short_updown signal")
        if rule.contract_family != SHORT_UPDOWN_FAMILY:
            raise ValueError("short challenger requires a short_updown rule")
        if checkpoint_lead_seconds not in self.checkpoints_seconds:
            raise ValueError("short challenger checkpoint was not predeclared")
        if signal.deadline is None or rule.target_time_utc is None:
            raise ValueError("short challenger requires a target time")
        if _utc(signal.deadline) != _utc(rule.target_time_utc):
            raise ValueError("short challenger signal/rule target mismatch")

        affirmative = self.repository.latest_market_snapshot_before(
            market_id=signal.market_id,
            outcome=rule.affirmative_outcome,
            received_at=signal.received_at,
        )
        negative = self.repository.latest_market_snapshot_before(
            market_id=signal.market_id,
            outcome=rule.negative_outcome,
            received_at=signal.received_at,
        )
        target = _utc(signal.deadline)
        observation = ShortChallengerObservation(
            observation_id=f"challenger:{uuid4()}",
            signal_id=signal.signal_id,
            market_id=signal.market_id,
            asset=signal.asset,
            target_time_utc=target,
            checkpoint_lead_seconds=checkpoint_lead_seconds,
            checkpoint_at=target - timedelta(seconds=checkpoint_lead_seconds),
            model_version=signal.model_version,
            model_probability=signal.estimated_probability,
            probability_low=signal.probability_low,
            probability_high=signal.probability_high,
            market_yes_midpoint=signal.yes_midpoint,
            market_no_midpoint=signal.no_midpoint,
            market_yes_ask_vwap=signal.yes_ask_vwap,
            market_no_ask_vwap=signal.no_ask_vwap,
            yes_spread=_row_decimal(affirmative, "spread"),
            no_spread=_row_decimal(negative, "spread"),
            yes_bid_depth=_row_decimal(affirmative, "bid_depth"),
            yes_ask_depth=_row_decimal(affirmative, "ask_depth"),
            no_bid_depth=_row_decimal(negative, "bid_depth"),
            no_ask_depth=_row_decimal(negative, "ask_depth"),
            yes_slippage=signal.yes_slippage_cost,
            no_slippage=signal.no_slippage_cost,
            target_size_usdc=signal.target_size_usdc,
            fee_rate=signal.fee_rate,
            selected_outcome=signal.selected_outcome,
            model_net_ev=signal.net_ev,
            status="captured" if signal.status == "analyzed" else "rejected",
            reasons=tuple(
                dict.fromkeys(
                    (
                        f"declared_checkpoint:T-{checkpoint_lead_seconds}s",
                        "sealed_v4_frozen_reference",
                        *signal.reasons,
                    )
                )
            ),
            observed_at=signal.observed_at,
            received_at=signal.received_at,
            source_version=SHORT_CHALLENGER_SOURCE_VERSION,
        )
        persisted, observation_created = (
            self.repository.save_short_challenger_observation(observation)
        )
        observation_id = str(persisted["observation_id"])
        replay_signal = (
            signal
            if observation_created
            else _signal_for_observation(signal, persisted)
        )
        created_count = 0
        entered_count = 0
        started_at = _utc(self.clock())
        started_monotonic = self.monotonic()

        for latency_ms in self.latencies_ms:
            if self.repository.has_short_latency_replay(
                observation_id=observation_id,
                latency_ms=latency_ms,
            ):
                continue
            target_elapsed = latency_ms / 1000
            remaining = target_elapsed - (self.monotonic() - started_monotonic)
            if remaining > 0 and replay_signal.selected_outcome in {"YES", "NO"}:
                self.sleeper(remaining)
            replay = self._capture_replay(
                observation_id=observation_id,
                signal=replay_signal,
                rule=rule,
                latency_ms=latency_ms,
                started_monotonic=started_monotonic,
                requested_at=started_at + timedelta(milliseconds=latency_ms),
            )
            _, created = self.repository.save_short_latency_replay(replay)
            if created:
                created_count += 1
                if replay.action == "enter":
                    entered_count += 1

        return ShortChallengerCaptureResult(
            observation_id=observation_id,
            observation_created=observation_created,
            replay_created_count=created_count,
            paper_entered_count=entered_count,
        )

    def settle_open(self, *, limit: int = 1000) -> int:
        settled = 0
        now = _utc(self.clock())
        for row in self.repository.settleable_open_short_latency_rows(limit=limit):
            outcome_yes = bool(row["settlement_outcome_yes"])
            outcome = str(row["outcome"])
            won = (outcome == "YES" and outcome_yes) or (
                outcome == "NO" and not outcome_yes
            )
            shares = Decimal(str(row["shares"]))
            cost = Decimal(str(row["size_usdc"]))
            total_fee = Decimal(str(row["total_fee"]))
            payout = shares if won else Decimal("0")
            self.repository.settle_short_latency_replay(
                replay_id=str(row["replay_id"]),
                label_id=str(row["settlement_label_id"]),
                outcome_yes=outcome_yes,
                payout_usdc=payout,
                pnl_usdc=payout - cost - total_fee,
                settled_at=now,
            )
            settled += 1
        return settled

    def _capture_replay(
        self,
        *,
        observation_id: str,
        signal: AnalysisSignal,
        rule: CryptoResolutionRule,
        latency_ms: int,
        started_monotonic: float,
        requested_at: datetime,
    ) -> ShortLatencyReplay:
        selected = signal.selected_outcome
        reasons: list[str] = []
        snapshot: OrderBookSnapshot | None = None
        execution: AskExecution | None = None
        payload_id: int | None = None
        if signal.status != "analyzed":
            reasons.append("signal_not_analyzed")
        if selected not in {"YES", "NO"}:
            reasons.append("no_selected_outcome")
        else:
            token_id = rule.yes_token_id if selected == "YES" else rule.no_token_id
            outcome = (
                rule.affirmative_outcome
                if selected == "YES"
                else rule.negative_outcome
            )
            if not token_id:
                reasons.append("missing_selected_token_id")
            else:
                try:
                    payload = self.client.get_order_book(token_id)
                    received_at = _utc(self.clock())
                    snapshot = translate_order_book(
                        market_id=signal.market_id,
                        token_id=token_id,
                        outcome=outcome,
                        payload=payload,
                        received_at=received_at,
                    )
                    payload_id = self.repository.record_external_payload(
                        market_id=signal.market_id,
                        source="polymarket_clob",
                        payload_kind=f"challenger_{outcome.lower()}_book",
                        payload=payload,
                        observed_at=snapshot.observed_at,
                        received_at=received_at,
                        source_version=CLOB_BOOK_SOURCE_VERSION,
                    )
                    self.repository.save_market_snapshot(snapshot)
                    reasons.extend(
                        self._snapshot_reasons(snapshot, received_at=received_at)
                    )
                    execution = calculate_ask_vwap(
                        snapshot.asks,
                        signal.target_size_usdc,
                    )
                    if not execution.complete:
                        reasons.extend(execution.reasons)
                except Exception as exc:
                    reasons.append(f"latency_book_error:{type(exc).__name__}")

        replay = _paper_replay(
            observation_id=observation_id,
            signal=signal,
            latency_ms=latency_ms,
            actual_latency_ms=max(
                0,
                int(round((self.monotonic() - started_monotonic) * 1000)),
            ),
            snapshot=snapshot,
            execution=execution,
            payload_id=payload_id,
            min_net_ev=self.min_net_ev,
            reasons=reasons,
            requested_at=requested_at,
            sampled_at=_utc(self.clock()),
        )
        return replay

    def _snapshot_reasons(
        self,
        snapshot: OrderBookSnapshot,
        *,
        received_at: datetime,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if not snapshot.timestamp_trusted:
            reasons.append("book_timestamp_untrusted")
        age = (_utc(received_at) - _utc(snapshot.observed_at)).total_seconds()
        if age < -5:
            reasons.append("book_timestamp_in_future")
        elif age > self.max_book_age_seconds:
            reasons.append(f"stale_book:{int(age)}s")
        if snapshot.best_bid is None or snapshot.best_ask is None:
            reasons.append("incomplete_book")
        return tuple(reasons)


def _paper_replay(
    *,
    observation_id: str,
    signal: AnalysisSignal,
    latency_ms: int,
    actual_latency_ms: int,
    snapshot: OrderBookSnapshot | None,
    execution: AskExecution | None,
    payload_id: int | None,
    min_net_ev: Decimal,
    reasons: list[str],
    requested_at: datetime,
    sampled_at: datetime,
) -> ShortLatencyReplay:
    outcome = signal.selected_outcome if signal.selected_outcome in {"YES", "NO"} else None
    probability = (
        signal.probability_low
        if outcome == "YES"
        else Decimal("1") - signal.probability_high
        if outcome == "NO" and signal.probability_high is not None
        else None
    )
    entry_vwap = execution.vwap if execution is not None else None
    shares = execution.shares if execution is not None and execution.shares > 0 else None
    fee_per_share: Decimal | None = None
    total_fee: Decimal | None = None
    net_ev: Decimal | None = None
    if signal.fee_rate is None or signal.fee_rate < 0:
        reasons.append("missing_fee_rate")
    elif entry_vwap is not None and shares is not None:
        total_fee = compute_taker_fee(
            shares=shares,
            price=entry_vwap,
            fee_rate=signal.fee_rate,
        )
        fee_per_share = total_fee / shares
    if probability is None:
        reasons.append("missing_conservative_probability")
    if entry_vwap is None:
        reasons.append("missing_executable_vwap")
    if probability is not None and entry_vwap is not None and fee_per_share is not None:
        net_ev = probability - entry_vwap - fee_per_share
        if net_ev < min_net_ev:
            reasons.append("net_ev_below_challenger_threshold")

    reasons = list(dict.fromkeys(reasons))
    action = "skip" if reasons else "enter"
    return ShortLatencyReplay(
        replay_id=f"latency:{uuid4()}",
        observation_id=observation_id,
        latency_ms=latency_ms,
        actual_latency_ms=actual_latency_ms,
        outcome=outcome,
        action=action,
        status="skipped" if action == "skip" else "open",
        size_usdc=signal.target_size_usdc,
        best_ask=snapshot.best_ask if snapshot is not None else None,
        entry_vwap=entry_vwap,
        fee_per_share=fee_per_share,
        shares=shares,
        total_fee=total_fee,
        net_ev=net_ev,
        payload_id=payload_id,
        reasons=tuple(reasons),
        requested_at=requested_at,
        sampled_at=sampled_at,
        source_version=SHORT_LATENCY_SOURCE_VERSION,
    )


def _row_decimal(row: Row | None, key: str) -> Decimal | None:
    if row is None or row[key] is None:
        return None
    return Decimal(str(row[key]))


def _signal_for_observation(signal: AnalysisSignal, row: Row) -> AnalysisSignal:
    """Recover a partial latency grid without changing its frozen decision."""
    return replace(
        signal,
        signal_id=str(row["signal_id"]),
        model_version=str(row["model_version"]),
        estimated_probability=_row_decimal(row, "model_probability"),
        probability_low=_row_decimal(row, "probability_low"),
        probability_high=_row_decimal(row, "probability_high"),
        target_size_usdc=Decimal(str(row["target_size_usdc"])),
        fee_rate=_row_decimal(row, "fee_rate"),
        selected_outcome=(
            str(row["selected_outcome"])
            if row["selected_outcome"] is not None
            else None
        ),
        net_ev=_row_decimal(row, "model_net_ev"),
        status="analyzed" if str(row["status"]) == "captured" else "rejected",
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        received_at=datetime.fromisoformat(str(row["received_at"])),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
