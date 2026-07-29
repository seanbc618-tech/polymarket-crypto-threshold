"""End-to-end read-only market analysis workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from crypto_threshold.adapters.polymarket.base import PolymarketReadClient
from crypto_threshold.adapters.polymarket.translator import (
    CLOB_BOOK_SOURCE_VERSION,
    GAMMA_SOURCE_VERSION,
    translate_market,
    translate_order_book,
)
from crypto_threshold.adapters.prices.binance import BinanceProvider
from crypto_threshold.adapters.prices.coinbase import CoinbaseProvider
from crypto_threshold.config import Settings
from crypto_threshold.domain.fees import (
    FEE_SOURCE_VERSION,
    MarketFeeSchedule,
    compute_taker_fee,
    parse_fee_schedule,
)
from crypto_threshold.domain.markets import (
    AskExecution,
    OrderBookSnapshot,
    calculate_ask_vwap,
)
from crypto_threshold.domain.prices import PriceCrossCheck, PriceSnapshot
from crypto_threshold.domain.probability import (
    DAILY_WORKFLOW_SOURCE_VERSION,
    SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION,
    AnalysisSignal,
    ProbabilityEstimate,
)
from crypto_threshold.domain.rules import (
    SHORT_UPDOWN_FAMILY,
    CryptoResolutionRule,
    parse_contract,
)
from crypto_threshold.services.cex_direction_service import (
    CEX_DIRECTION_ARTIFACT_VERSION,
    CEX_DIRECTION_FEATURE_NAMES,
    CEX_DIRECTION_MODEL_NAME,
    CEX_DIRECTION_SUPPORTED_ASSETS,
    CexDirectionArtifact,
    extract_cex_direction_features,
)
from crypto_threshold.services.pricing_service import cross_check_prices
from crypto_threshold.services.probability_service import (
    annualized_realized_volatility,
    estimate_threshold_probability,
)
from crypto_threshold.services.stream_research_service import (
    StreamPulseResult,
    StreamResearchCoordinator,
)
from crypto_threshold.storage.repositories import Repository

WORKFLOW_VERSION = DAILY_WORKFLOW_SOURCE_VERSION


@dataclass
class _AnalysisAudit:
    run_id: str
    payloads: list[tuple[int, str]] = field(default_factory=list)


class MarketWorkflowService:
    """The only production owner of market research and net-EV analysis."""

    def __init__(
        self,
        *,
        client: PolymarketReadClient,
        repository: Repository,
        binance: BinanceProvider,
        coinbase: CoinbaseProvider,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
        stream_coordinator: StreamResearchCoordinator | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.binance = binance
        self.coinbase = coinbase
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.stream_coordinator = stream_coordinator
        self._cex_direction_artifact: CexDirectionArtifact | None = None

    def analyze(
        self, market_id: str, *, target_size_usdc: Decimal | None = None
    ) -> AnalysisSignal:
        audit = _AnalysisAudit(run_id=f"analysis:{uuid4()}")
        target = (
            self.settings.ANALYSIS_SIZE_USDC
            if target_size_usdc is None
            else target_size_usdc
        )
        payload = self.client.get_market(market_id)
        now = _utc(self.clock())
        market = translate_market(payload, received_at=now)
        if not market.market_id:
            market = translate_market({**payload, "id": market_id}, received_at=now)
        self.repository.upsert_market(market)
        self._record(
            audit,
            market.market_id,
            "gamma",
            "market",
            payload,
            now,
            now,
            GAMMA_SOURCE_VERSION,
        )
        preflight_reasons: list[str] = []
        if not market.event_id:
            try:
                context = self.client.get_market_event_context(
                    market.market_id,
                    market.condition_id,
                    market.question,
                )
                now = _utc(self.clock())
                self._record(
                    audit,
                    market.market_id,
                    "gamma",
                    "event_context",
                    context.raw_payload,
                    now,
                    now,
                    GAMMA_SOURCE_VERSION,
                )
                if context.event_id:
                    market = replace(market, event_id=context.event_id)
                    self.repository.upsert_market(market)
            except Exception as exc:
                preflight_reasons.append(f"event_context_error:{type(exc).__name__}")
        rule = parse_contract(market, now=now)
        self.repository.save_resolution_rule(
            market.market_id,
            rule,
            observed_at=now,
            received_at=now,
        )
        if self.stream_coordinator is not None:
            try:
                self.stream_coordinator.sync_subscriptions(
                    active_market_ids=(market.market_id,)
                )
            except Exception:
                # The optional stream may never block the authoritative REST path.
                pass
        reasons = preflight_reasons + list(rule.rejection_reasons)
        if rule.contract_family == SHORT_UPDOWN_FAMILY and not reasons:
            reasons.extend(self._short_cex_preflight(rule, now=now))
        if reasons:
            return self._save_rejected(
                market.market_id, rule, target, reasons, now, audit=audit
            )

        affirmative = rule.affirmative_outcome.upper()
        negative = rule.negative_outcome.upper()
        yes_book = self._book(
            market.market_id, rule.yes_token_id, affirmative, reasons, audit
        )
        no_book = self._book(
            market.market_id, rule.no_token_id, negative, reasons, audit
        )
        fee_schedule = self._fee_schedule(market.market_id, rule, reasons, audit)
        threshold: Decimal | None = rule.strike
        secondary: PriceSnapshot | None = None
        estimate: ProbabilityEstimate | None = None
        if rule.contract_family == SHORT_UPDOWN_FAMILY:
            threshold = None
            primary, estimate = self._cex_direction_inputs(
                market.market_id, rule, reasons, audit
            )
        else:
            primary, volatility = self._binance_inputs(
                market.market_id, rule, reasons, audit
            )
            secondary = self._coinbase_input(market.market_id, rule, reasons, audit)
        decision_at = _utc(self.clock())

        cross_check: PriceCrossCheck | None = None
        if primary is not None and secondary is not None:
            cross_check = cross_check_prices(
                primary,
                secondary,
                max_diff=self.settings.PRICE_CROSSCHECK_MAX_DIFF,
                max_age_seconds=self.settings.MAX_PRICE_AGE_SECONDS,
                now=decision_at,
            )
            self.repository.save_price_cross_check(cross_check, market_id=market.market_id)
            reasons.extend(
                reason
                for reason in cross_check.reasons
                if reason != "quote_basis_usdt_usd"
            )
            if not cross_check.ok:
                reasons.append("price_cross_check_failed")

        if (
            rule.contract_family != SHORT_UPDOWN_FAMILY
            and primary is not None
            and threshold is not None
            and threshold > 0
            and rule.target_time_utc is not None
        ):
            hours = Decimal(
                str((rule.target_time_utc - decision_at).total_seconds() / 3600)
            )
            estimate = estimate_threshold_probability(
                spot_price=primary.price,
                threshold=threshold,
                time_to_deadline_hours=hours,
                realized_volatility=volatility,
                operator=rule.exact_operator,
            )
            if not estimate.accepted:
                reasons.append(estimate.rejection_reason or "probability_rejected")
        if (
            rule.contract_family == SHORT_UPDOWN_FAMILY
            and rule.target_time_utc is not None
            and decision_at >= rule.target_time_utc
        ):
            reasons.append("cex_direction_analysis_completed_after_deadline")

        yes_execution = calculate_ask_vwap(yes_book.asks, target) if yes_book else None
        no_execution = calculate_ask_vwap(no_book.asks, target) if no_book else None
        for outcome, execution in (("yes", yes_execution), ("no", no_execution)):
            if execution is None:
                reasons.append(f"missing_{outcome}_execution")
            elif not execution.complete:
                reasons.extend(f"{outcome}_{reason}" for reason in execution.reasons)

        reasons = list(dict.fromkeys(reasons))
        complete_inputs: tuple[object | None, ...] = (
            yes_book,
            no_book,
            fee_schedule,
            primary,
            estimate,
            yes_execution,
            no_execution,
        )
        if rule.contract_family != SHORT_UPDOWN_FAMILY:
            complete_inputs += (secondary,)
        if reasons or not all(complete_inputs):
            return self._save_rejected(
                market.market_id,
                rule,
                target,
                reasons or ["incomplete_analysis_inputs"],
                decision_at,
                estimate=estimate,
                yes_book=yes_book,
                no_book=no_book,
                yes_execution=yes_execution,
                no_execution=no_execution,
                fee_schedule=fee_schedule,
                threshold=threshold,
                audit=audit,
            )

        assert estimate is not None and estimate.base_probability is not None
        assert yes_execution is not None and yes_execution.vwap is not None
        assert no_execution is not None and no_execution.vwap is not None
        assert yes_book is not None and no_book is not None
        assert fee_schedule is not None and fee_schedule.fee_rate is not None

        yes_fee = _execution_fee_per_share(
            yes_execution, fee_rate=fee_schedule.fee_rate
        )
        no_fee = _execution_fee_per_share(
            no_execution, fee_rate=fee_schedule.fee_rate
        )
        yes_spread = _spread_cost(yes_book)
        no_spread = _spread_cost(no_book)
        yes_slippage = yes_execution.slippage_per_share or Decimal("0")
        no_slippage = no_execution.slippage_per_share or Decimal("0")
        yes_probability_for_ev = (
            estimate.probability_low
            if rule.contract_family == SHORT_UPDOWN_FAMILY
            and estimate.probability_low is not None
            else estimate.base_probability
        )
        no_probability_for_ev = (
            Decimal("1") - estimate.probability_high
            if rule.contract_family == SHORT_UPDOWN_FAMILY
            and estimate.probability_high is not None
            else Decimal("1") - estimate.base_probability
        )
        yes_net = (
            yes_probability_for_ev
            - (yes_book.midpoint or Decimal("0"))
            - yes_spread
            - yes_slippage
            - yes_fee
        )
        no_net = (
            no_probability_for_ev
            - (no_book.midpoint or Decimal("0"))
            - no_spread
            - no_slippage
            - no_fee
        )
        selected = "YES" if yes_net >= no_net and yes_net > 0 else (
            "NO" if no_net > yes_net and no_net > 0 else None
        )
        net_ev = yes_net if selected == "YES" else no_net if selected == "NO" else max(
            yes_net, no_net
        )
        signal = AnalysisSignal(
            signal_id=f"signal:{uuid4()}",
            market_id=market.market_id,
            asset=rule.asset,
            threshold=threshold,
            deadline=rule.target_time_utc,
            estimated_probability=estimate.base_probability,
            probability_low=estimate.probability_low,
            probability_high=estimate.probability_high,
            yes_midpoint=yes_book.midpoint,
            no_midpoint=no_book.midpoint,
            yes_ask_vwap=yes_execution.vwap,
            no_ask_vwap=no_execution.vwap,
            target_size_usdc=target,
            fee_rate=fee_schedule.fee_rate,
            yes_fee_per_share=yes_fee,
            no_fee_per_share=no_fee,
            yes_spread_cost=yes_spread,
            no_spread_cost=no_spread,
            yes_slippage_cost=yes_slippage,
            no_slippage_cost=no_slippage,
            yes_net_ev=yes_net,
            no_net_ev=no_net,
            selected_outcome=selected,
            net_ev=net_ev,
            status="analyzed",
            model_name=estimate.model_name,
            model_version=estimate.model_version,
            confidence=estimate.confidence,
            reasons=tuple(estimate.reasons) + (("no_positive_net_ev",) if selected is None else ()),
            observed_at=decision_at,
            received_at=decision_at,
            source_version=_workflow_source_version(rule),
            contract_family=rule.contract_family,
            affirmative_outcome=rule.affirmative_outcome,
            negative_outcome=rule.negative_outcome,
        )
        self.repository.save_analysis_signal(
            signal,
            analysis_run_id=audit.run_id,
            input_payloads=tuple(audit.payloads),
        )
        return signal

    def short_signal_due(
        self,
        rule: CryptoResolutionRule,
        *,
        at: datetime,
    ) -> bool:
        """Return whether the sealed short-model checkpoint is actionable now."""
        return (
            rule.contract_family == SHORT_UPDOWN_FAMILY
            and not rule.rejection_reasons
            and not self._short_cex_preflight(rule, now=_utc(at))
        )

    def _book(
        self,
        market_id: str,
        token_id: str | None,
        outcome: str,
        reasons: list[str],
        audit: _AnalysisAudit,
    ) -> OrderBookSnapshot | None:
        if not token_id:
            reasons.append(f"missing_{outcome.lower()}_token_id")
            return None
        try:
            payload = self.client.get_order_book(token_id)
            received_at = _utc(self.clock())
            snapshot = translate_order_book(
                market_id=market_id,
                token_id=token_id,
                outcome=outcome,
                payload=payload,
                received_at=received_at,
            )
            self._record(
                audit,
                market_id,
                "polymarket_clob",
                f"{outcome.lower()}_book",
                payload,
                snapshot.observed_at,
                received_at,
                CLOB_BOOK_SOURCE_VERSION,
            )
            self.repository.save_market_snapshot(snapshot)
            book_valid = True
            if not snapshot.timestamp_trusted:
                reasons.append(f"{outcome.lower()}_book_timestamp_untrusted")
                book_valid = False
            age = (received_at - snapshot.observed_at).total_seconds()
            if age < -5:
                reasons.append(f"{outcome.lower()}_book_timestamp_in_future")
                book_valid = False
            elif age > self.settings.MAX_BOOK_AGE_SECONDS:
                reasons.append(f"stale_{outcome.lower()}_book:{int(age)}s")
                book_valid = False
            if snapshot.best_bid is None or snapshot.best_ask is None:
                reasons.append(f"incomplete_{outcome.lower()}_book")
                book_valid = False
            if book_valid and self.stream_coordinator is not None:
                self.stream_coordinator.mark_rest_verified(token_id)
            return snapshot
        except Exception as exc:
            reasons.append(f"{outcome.lower()}_book_error:{type(exc).__name__}")
            return None

    def analyze_stream_pulse(
        self,
    ) -> tuple[StreamPulseResult | None, tuple[AnalysisSignal, ...]]:
        """Reanalyze changed complete ladders through the same REST workflow.

        Stream quotes only choose which ladder is due. Every member analysis
        still fetches token-specific REST books and never combines stream BBOs
        with persisted sibling snapshots.
        """
        if self.stream_coordinator is None:
            return None, ()
        pulse = self.stream_coordinator.pulse()
        signals: list[AnalysisSignal] = []
        for _ladder, market_ids in pulse.reprice_ladders:
            for market_id in market_ids:
                signals.append(self.analyze(market_id))
        return pulse, tuple(signals)

    def _fee_schedule(
        self,
        market_id: str,
        rule: CryptoResolutionRule,
        reasons: list[str],
        audit: _AnalysisAudit,
    ) -> MarketFeeSchedule | None:
        if not rule.condition_id:
            reasons.append("missing_condition_id_for_fee_schedule")
            return None
        try:
            payload = self.client.get_market_info(rule.condition_id)
            received_at = _utc(self.clock())
            self._record(
                audit,
                market_id,
                "polymarket_clob",
                "market_info_fee_schedule",
                payload,
                received_at,
                received_at,
                FEE_SOURCE_VERSION,
            )
            schedule = parse_fee_schedule(
                market_id=market_id,
                condition_id=rule.condition_id,
                payload=payload,
                observed_at=received_at,
                received_at=received_at,
            )
            self.repository.save_fee_schedule(schedule)
            if not schedule.valid:
                reasons.append(schedule.rejection_reason or "missing_fee_schedule")
            return schedule
        except Exception as exc:
            reasons.append(f"fee_schedule_error:{type(exc).__name__}")
            return None

    def _binance_inputs(
        self,
        market_id: str,
        rule: CryptoResolutionRule,
        reasons: list[str],
        audit: _AnalysisAudit,
    ) -> tuple[PriceSnapshot | None, Decimal | None]:
        try:
            settlement_series = self.binance.get_klines(rule.asset, interval="1m", limit=2)
            primary = self.binance.latest_close_snapshot(
                settlement_series, now=_utc(self.clock())
            )
            self._record(
                audit,
                market_id,
                "binance",
                "settlement_klines_1m",
                settlement_series.raw_payload,
                primary.observed_at,
                settlement_series.received_at,
                settlement_series.source_version,
            )
            self.repository.save_price_snapshot(primary, market_id=market_id)

            history = self.binance.get_klines(rule.asset, interval="1d", limit=31)
            history_cutoff = _utc(self.clock())
            closed_history = replace(
                history,
                klines=tuple(
                    kline for kline in history.klines if kline.close_time <= history_cutoff
                ),
            )
            self._record(
                audit,
                market_id,
                "binance",
                "volatility_klines_1d",
                history.raw_payload,
                closed_history.klines[-1].close_time if closed_history.klines else None,
                history.received_at,
                history.source_version,
            )
            volatility = annualized_realized_volatility(closed_history)
            if volatility is None:
                reasons.append("insufficient_volatility_history")
            if primary.symbol != (rule.pair or "").replace("/", ""):
                reasons.append("binance_pair_mismatch")
            if primary.price_kind != "1m_close":
                reasons.append("binance_candle_field_mismatch")
            age = (_utc(self.clock()) - primary.observed_at).total_seconds()
            if age > self.settings.MAX_PRICE_AGE_SECONDS:
                reasons.append(f"stale_binance_price:{int(age)}s")
            return primary, volatility
        except Exception as exc:
            reasons.append(f"binance_input_error:{type(exc).__name__}")
            return None, None

    def _coinbase_input(
        self,
        market_id: str,
        rule: CryptoResolutionRule,
        reasons: list[str],
        audit: _AnalysisAudit,
    ) -> PriceSnapshot | None:
        try:
            snapshot = self.coinbase.get_spot_price(rule.asset)
            self._record(
                audit,
                market_id,
                "coinbase",
                "sanity_spot",
                snapshot.raw_payload,
                snapshot.observed_at,
                snapshot.received_at,
                snapshot.source_version,
            )
            self.repository.save_price_snapshot(snapshot, market_id=market_id)
            if snapshot.asset != rule.asset:
                reasons.append("coinbase_asset_mismatch")
            if snapshot.quote != "USD":
                reasons.append("coinbase_quote_mismatch")
            age = (_utc(self.clock()) - snapshot.observed_at).total_seconds()
            if age > self.settings.MAX_PRICE_AGE_SECONDS:
                reasons.append("stale_coinbase_price")
            return snapshot
        except Exception as exc:
            reasons.append(f"coinbase_input_error:{type(exc).__name__}")
            return None

    def _short_cex_preflight(
        self,
        rule: CryptoResolutionRule,
        *,
        now: datetime,
    ) -> list[str]:
        reasons: list[str] = []
        if rule.asset not in CEX_DIRECTION_SUPPORTED_ASSETS:
            reasons.append(f"cex_direction_unsupported_asset:{rule.asset}")
            return reasons
        target = rule.target_time_utc
        window_start = rule.window_start_time_utc
        if target is None or window_start is None:
            reasons.append("missing_cex_direction_contract_window")
            return reasons
        try:
            artifact = self._cex_artifact()
        except (FileNotFoundError, ValueError, OSError) as exc:
            reasons.append(f"cex_direction_model_unavailable:{type(exc).__name__}")
            return reasons
        checkpoint = target - timedelta(seconds=artifact.decision_lead_seconds)
        current = _utc(now)
        if current < checkpoint:
            reasons.append("cex_direction_checkpoint_not_reached")
            return reasons
        remaining = (target - current).total_seconds()
        checkpoint_lag = (current - checkpoint).total_seconds()
        if remaining < self.settings.SHORT_CEX_MIN_REMAINING_SECONDS:
            reasons.append("cex_direction_checkpoint_too_late")
        if checkpoint_lag > self.settings.SHORT_CEX_MAX_CHECKPOINT_LAG_SECONDS:
            reasons.append("cex_direction_checkpoint_expired")
        return reasons

    def _cex_direction_inputs(
        self,
        market_id: str,
        rule: CryptoResolutionRule,
        reasons: list[str],
        audit: _AnalysisAudit,
    ) -> tuple[PriceSnapshot | None, ProbabilityEstimate | None]:
        target = rule.target_time_utc
        window_start = rule.window_start_time_utc
        interval = rule.candle_interval
        if target is None or window_start is None or interval is None:
            reasons.append("missing_cex_direction_contract_window")
            return None, None
        try:
            artifact = self._cex_artifact()
            checkpoint = target - timedelta(seconds=artifact.decision_lead_seconds)
            series = self.binance.get_klines(
                rule.asset,
                interval="1m",
                limit=64,
                start_time=checkpoint - timedelta(minutes=30),
                end_time=checkpoint,
            )
            features = extract_cex_direction_features(
                series,
                asset=rule.asset,
                interval=interval,
                window_start_time_utc=window_start,
                checkpoint_at=checkpoint,
            )
        except Exception as exc:
            reasons.append(f"cex_direction_input_error:{type(exc).__name__}")
            return None, None

        self._record(
            audit,
            market_id,
            "binance",
            "cex_direction_klines_1m",
            series.raw_payload,
            features.latest_close_time,
            series.received_at,
            series.source_version,
        )
        self._record(
            audit,
            market_id,
            "local_model",
            "cex_direction_model",
            artifact.as_payload(),
            _training_cutoff(artifact),
            _utc(self.clock()),
            CEX_DIRECTION_ARTIFACT_VERSION,
        )
        snapshot = PriceSnapshot(
            asset=rule.asset,
            quote="USDT",
            provider="binance",
            symbol=series.symbol,
            price=Decimal(str(features.latest_close)),
            price_kind=f"cex_direction_checkpoint_T-{artifact.decision_lead_seconds}s",
            observed_at=features.latest_close_time,
            received_at=series.received_at,
            source_version=series.source_version,
            raw_payload=series.raw_payload,
        )
        self.repository.save_price_snapshot(snapshot, market_id=market_id)

        probability_value = artifact.predict(features)
        probability = Decimal(str(probability_value))
        margin = Decimal(str(artifact.probability_margin))
        probability_low = max(Decimal("0"), probability - margin)
        probability_high = min(Decimal("1"), probability + margin)
        decision_at = _utc(self.clock())
        hours = Decimal(str((target - decision_at).total_seconds() / 3600))
        volatility_index = CEX_DIRECTION_FEATURE_NAMES.index(
            "realized_volatility_10"
        )
        volatility = Decimal(str(features.values[volatility_index]))
        distance = abs(probability - Decimal("0.5"))
        confidence = (
            "high"
            if distance >= Decimal("0.20")
            else "medium"
            if distance >= Decimal("0.10")
            else "low"
        )
        estimate = ProbabilityEstimate(
            accepted=True,
            rejection_reason=None,
            threshold=None,
            spot_price=snapshot.price,
            time_to_deadline_hours=hours,
            base_probability=probability,
            probability_low=probability_low,
            probability_high=probability_high,
            realized_volatility=volatility,
            model_name=CEX_DIRECTION_MODEL_NAME,
            model_version=artifact.runtime_model_version,
            confidence=confidence,
            reasons=(
                f"closed_cex_checkpoint:T-{artifact.decision_lead_seconds}s",
                "chainlink_outcome_is_target_only",
                "conservative_probability_bounds_for_net_ev",
                f"artifact_hash:{artifact.artifact_hash}",
            ),
        )
        return snapshot, estimate

    def _cex_artifact(self) -> CexDirectionArtifact:
        if self._cex_direction_artifact is None:
            self._cex_direction_artifact = CexDirectionArtifact.load(
                self.settings.SHORT_CEX_MODEL_PATH
            )
        return self._cex_direction_artifact

    def _save_rejected(
        self,
        market_id: str,
        rule: CryptoResolutionRule,
        target: Decimal,
        reasons: list[str],
        now: datetime,
        *,
        estimate: ProbabilityEstimate | None = None,
        yes_book: OrderBookSnapshot | None = None,
        no_book: OrderBookSnapshot | None = None,
        yes_execution: AskExecution | None = None,
        no_execution: AskExecution | None = None,
        fee_schedule: MarketFeeSchedule | None = None,
        threshold: Decimal | None = None,
        audit: _AnalysisAudit,
    ) -> AnalysisSignal:
        effective_threshold = threshold if threshold and threshold > 0 else rule.strike
        signal = AnalysisSignal(
            signal_id=f"signal:{uuid4()}",
            market_id=market_id,
            asset=rule.asset,
            threshold=effective_threshold if effective_threshold > 0 else None,
            deadline=rule.target_time_utc,
            estimated_probability=estimate.base_probability if estimate else None,
            probability_low=estimate.probability_low if estimate else None,
            probability_high=estimate.probability_high if estimate else None,
            yes_midpoint=yes_book.midpoint if yes_book else None,
            no_midpoint=no_book.midpoint if no_book else None,
            yes_ask_vwap=yes_execution.vwap if yes_execution else None,
            no_ask_vwap=no_execution.vwap if no_execution else None,
            target_size_usdc=target,
            fee_rate=fee_schedule.fee_rate if fee_schedule else None,
            yes_fee_per_share=None,
            no_fee_per_share=None,
            yes_spread_cost=_spread_cost(yes_book) if yes_book else None,
            no_spread_cost=_spread_cost(no_book) if no_book else None,
            yes_slippage_cost=yes_execution.slippage_per_share if yes_execution else None,
            no_slippage_cost=no_execution.slippage_per_share if no_execution else None,
            yes_net_ev=None,
            no_net_ev=None,
            selected_outcome=None,
            net_ev=None,
            status="rejected",
            model_name=estimate.model_name if estimate else "not_computed",
            model_version=estimate.model_version if estimate else "not_computed",
            confidence="rejected",
            reasons=tuple(dict.fromkeys(reasons)),
            observed_at=now,
            received_at=now,
            source_version=_workflow_source_version(rule),
            contract_family=rule.contract_family,
            affirmative_outcome=rule.affirmative_outcome,
            negative_outcome=rule.negative_outcome,
        )
        self.repository.save_analysis_signal(
            signal,
            analysis_run_id=audit.run_id,
            input_payloads=tuple(audit.payloads),
        )
        return signal

    def _record(
        self,
        audit: _AnalysisAudit,
        market_id: str,
        source: str,
        kind: str,
        payload: object,
        observed_at: datetime | None,
        received_at: datetime,
        source_version: str,
    ) -> int:
        payload_id = self.repository.record_external_payload(
            market_id=market_id,
            source=source,
            payload_kind=kind,
            payload=payload,
            observed_at=observed_at,
            received_at=received_at,
            source_version=source_version,
            analysis_run_id=audit.run_id,
        )
        audit.payloads.append((payload_id, kind))
        return payload_id


def _spread_cost(snapshot: OrderBookSnapshot) -> Decimal:
    if snapshot.best_ask is None or snapshot.midpoint is None:
        return Decimal("0")
    return max(Decimal("0"), snapshot.best_ask - snapshot.midpoint)


def _execution_fee_per_share(
    execution: AskExecution, *, fee_rate: Decimal
) -> Decimal:
    if execution.vwap is None or execution.shares <= 0:
        return Decimal("0")
    total = compute_taker_fee(
        shares=execution.shares,
        price=execution.vwap,
        fee_rate=fee_rate,
    )
    return total / execution.shares


def _training_cutoff(artifact: CexDirectionArtifact) -> datetime | None:
    value = artifact.training.get("training_cutoff_time_utc")
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _workflow_source_version(rule: CryptoResolutionRule) -> str:
    if rule.contract_family == SHORT_UPDOWN_FAMILY:
        return SHORT_UPDOWN_WORKFLOW_SOURCE_VERSION
    return DAILY_WORKFLOW_SOURCE_VERSION
