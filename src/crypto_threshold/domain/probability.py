"""Probability estimate and persisted analysis signal models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class ProbabilityEstimate:
    accepted: bool
    rejection_reason: str | None
    threshold: Decimal
    spot_price: Decimal
    time_to_deadline_hours: Decimal
    base_probability: Decimal | None
    probability_low: Decimal | None
    probability_high: Decimal | None
    realized_volatility: Decimal | None
    model_name: str = "gbm_terminal_threshold"
    model_version: str = "gbm-terminal-v2"
    confidence: str = "low"
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AnalysisSignal:
    signal_id: str
    market_id: str
    asset: str
    threshold: Decimal | None
    deadline: datetime | None
    estimated_probability: Decimal | None
    probability_low: Decimal | None
    probability_high: Decimal | None
    yes_midpoint: Decimal | None
    no_midpoint: Decimal | None
    yes_ask_vwap: Decimal | None
    no_ask_vwap: Decimal | None
    target_size_usdc: Decimal
    fee_rate: Decimal | None
    yes_fee_per_share: Decimal | None
    no_fee_per_share: Decimal | None
    yes_spread_cost: Decimal | None
    no_spread_cost: Decimal | None
    yes_slippage_cost: Decimal | None
    no_slippage_cost: Decimal | None
    yes_net_ev: Decimal | None
    no_net_ev: Decimal | None
    selected_outcome: str | None
    net_ev: Decimal | None
    status: str
    model_name: str
    model_version: str
    confidence: str
    reasons: tuple[str, ...]
    observed_at: datetime
    received_at: datetime
    source_version: str = "market-workflow-v1"
