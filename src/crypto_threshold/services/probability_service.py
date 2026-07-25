"""Transparent terminal-threshold probability model."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from crypto_threshold.domain.prices import KlineSeries
from crypto_threshold.domain.probability import ProbabilityEstimate


def estimate_threshold_probability(
    *,
    spot_price: Decimal,
    threshold: Decimal,
    time_to_deadline_hours: Decimal,
    realized_volatility: Decimal | None = None,
    operator: str = ">",
) -> ProbabilityEstimate:
    """Estimate a terminal threshold probability or return a rejection result."""
    reasons: list[str] = []
    if spot_price <= 0:
        return _rejected("spot_price_non_positive", spot_price, threshold, time_to_deadline_hours)
    if threshold <= 0:
        return _rejected("threshold_non_positive", spot_price, threshold, time_to_deadline_hours)
    if time_to_deadline_hours <= 0:
        return _rejected(
            "time_to_deadline_non_positive", spot_price, threshold, time_to_deadline_hours
        )
    if operator not in {">", "<", ">=", "<="}:
        return _rejected("unsupported_operator", spot_price, threshold, time_to_deadline_hours)

    confidence = "medium"
    if realized_volatility is None or realized_volatility <= 0:
        realized_volatility = Decimal("0.80")
        confidence = "low"
        reasons.append("default_volatility_used")
    else:
        reasons.append(f"realized_volatility={realized_volatility}")

    t_years = float(time_to_deadline_hours) / (365.25 * 24)
    vol = float(realized_volatility)
    probabilities = [
        _terminal_probability(
            spot=float(spot_price),
            strike=float(threshold),
            years=t_years,
            volatility=scenario_vol,
            operator=operator,
        )
        for scenario_vol in (vol * 0.8, vol, vol * 1.2)
    ]
    base = probabilities[1]
    low = min(probabilities)
    high = max(probabilities)

    hours = float(time_to_deadline_hours)
    if hours < 1:
        confidence = "low"
        reasons.append("deadline_under_one_hour")
    elif hours < 24 and confidence != "low":
        confidence = "low-medium"
        reasons.append("deadline_under_24_hours")

    return ProbabilityEstimate(
        accepted=True,
        rejection_reason=None,
        threshold=threshold,
        spot_price=spot_price,
        time_to_deadline_hours=time_to_deadline_hours,
        base_probability=_probability_decimal(base),
        probability_low=_probability_decimal(low),
        probability_high=_probability_decimal(high),
        realized_volatility=realized_volatility,
        confidence=confidence,
        reasons=tuple(reasons),
    )


def annualized_realized_volatility(series: KlineSeries) -> Decimal | None:
    """Annualize daily close-to-close log-return standard deviation."""
    closes = [float(kline.close) for kline in series.klines if kline.close > 0]
    if len(closes) < 3:
        return None
    returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:])]
    if len(returns) < 2:
        return None
    periods = 365 if series.interval == "1d" else 365 * 24 * 60
    return Decimal(str(statistics.stdev(returns) * math.sqrt(periods)))


def annualized_tick_volatility(
    observations: Sequence[tuple[datetime, Decimal]],
    *,
    sample_seconds: int = 5,
    min_samples: int = 12,
) -> Decimal | None:
    """Annualize log returns after deterministic fixed-width downsampling."""
    if sample_seconds < 1 or min_samples < 3:
        raise ValueError("invalid tick volatility bounds")
    buckets: dict[int, Decimal] = {}
    for observed_at, price in sorted(observations, key=lambda item: item[0]):
        if price <= 0:
            continue
        bucket = int(observed_at.timestamp()) // sample_seconds
        buckets[bucket] = price
    prices = [float(price) for _, price in sorted(buckets.items())]
    if len(prices) < min_samples:
        return None
    returns = [
        math.log(current / previous)
        for previous, current in zip(prices, prices[1:])
        if previous > 0 and current > 0
    ]
    if len(returns) < min_samples - 1:
        return None
    periods_per_year = (365.25 * 24 * 60 * 60) / sample_seconds
    return Decimal(str(statistics.stdev(returns) * math.sqrt(periods_per_year)))


def _terminal_probability(
    *, spot: float, strike: float, years: float, volatility: float, operator: str
) -> float:
    d2 = (math.log(spot / strike) - 0.5 * volatility * volatility * years) / (
        volatility * math.sqrt(years)
    )
    above = _norm_cdf(d2)
    return above if operator in {">", ">="} else 1.0 - above


def _norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2)))


def _probability_decimal(value: float) -> Decimal:
    return Decimal(str(round(max(0.001, min(0.999, value)), 6)))


def _rejected(
    reason: str,
    spot_price: Decimal,
    threshold: Decimal,
    time_to_deadline_hours: Decimal,
) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        accepted=False,
        rejection_reason=reason,
        threshold=threshold,
        spot_price=spot_price,
        time_to_deadline_hours=time_to_deadline_hours,
        base_probability=None,
        probability_low=None,
        probability_high=None,
        realized_volatility=None,
        confidence="rejected",
        reasons=(reason,),
    )
