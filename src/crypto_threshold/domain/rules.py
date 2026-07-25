"""Authoritative parser for supported Polymarket crypto contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from crypto_threshold.domain.assets import (
    ASSET_CONTRACTS,
    SHORT_UPDOWN_ASSETS,
    SUPPORTED_ASSETS,
    asset_contract,
)
from crypto_threshold.domain.markets import CryptoMarket

PARSER_VERSION = "3.0.0"
DAILY_THRESHOLD_FAMILY = "daily_threshold"
SHORT_UPDOWN_FAMILY = "short_updown"
SETTLEMENT_TIMEZONE = "America/New_York"

ASSET_ALIASES = {
    alias: symbol
    for symbol, contract in ASSET_CONTRACTS.items()
    for alias in contract.aliases
}

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class CryptoResolutionRule:
    """Complete settlement contract required by the read-only workflow."""

    event_id: str | None
    condition_id: str | None
    yes_token_id: str | None
    no_token_id: str | None
    asset: str
    settlement_provider: str | None
    pair: str | None
    exact_operator: str
    strike: Decimal
    candle_interval: str | None
    price_field: str | None
    timezone: str | None
    observation_time: str | None
    target_time_utc: datetime | None
    gamma_end_date: datetime | None
    parser_version: str
    raw_description: str
    question: str
    rule_confidence: float
    tradable: bool
    preview_only: bool
    rejection_reasons: tuple[str, ...]
    contract_family: str = DAILY_THRESHOLD_FAMILY
    boundary_type: str = "fixed_strike"
    window_start_time_utc: datetime | None = None
    affirmative_outcome: str = "Yes"
    negative_outcome: str = "No"
    series_slug: str | None = None

    @property
    def rejection_reason(self) -> str | None:
        return "; ".join(self.rejection_reasons) if self.rejection_reasons else None

    @property
    def operator(self) -> str:
        return self.exact_operator

    @property
    def threshold(self) -> Decimal:
        return self.strike

    @property
    def settlement_source(self) -> str | None:
        return self.settlement_provider

    @property
    def quote(self) -> str:
        if self.pair and "/" in self.pair:
            return self.pair.split("/", 1)[1]
        return ""

    @property
    def raw_text(self) -> str:
        return self.question if not self.raw_description else (
            f"{self.question} | {self.raw_description}"
        )


def parse_contract(
    market: CryptoMarket,
    *,
    now: datetime | None = None,
) -> CryptoResolutionRule:
    """Parse and gate a Gamma market using its binding description."""
    now = _as_utc(now or datetime.now(UTC))
    normalized_outcomes = tuple(outcome.strip().lower() for outcome in market.outcomes)
    if set(normalized_outcomes) == {"up", "down"}:
        return _parse_short_updown_contract(market, now=now)

    question = market.question.strip()
    description = (market.description or "").strip()
    combined = f"{question}\n{description}"

    asset = _detect_asset(question) or _detect_asset(description) or ""
    operator = _detect_operator(question)
    strike = _extract_threshold(question) or Decimal("0")
    provider = _extract_provider(description)
    pair = _extract_pair(description)
    interval = _extract_candle_interval(description)
    price_field = _extract_price_field(description)
    timezone = _extract_timezone(description)
    observation = _extract_observation_time(description)
    target_date, explicit_year = _extract_date(question, now=now)
    target_time = _target_time_utc(target_date, observation, timezone)

    reasons: list[str] = []
    required = {
        "event_id": market.event_id,
        "condition_id": market.condition_id,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "asset": asset,
        "settlement_provider": provider,
        "pair": pair,
        "exact_operator": operator,
        "strike": strike if strike > 0 else None,
        "candle_interval": interval,
        "price_field": price_field,
        "timezone": timezone,
        "observation_time": observation,
        "target_time_utc": target_time,
        "gamma_end_date": market.gamma_end_date,
        "raw_description": description,
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        reasons.append(f"missing_contract_fields:{','.join(missing)}")

    if _has_path_dependent_language(combined):
        reasons.append("unsupported_path_dependent_contract")
    if re.search(r"\bbetween\b|\brange\b", combined, re.I):
        reasons.append("unsupported_range_contract")
    if len(normalized_outcomes) != 2 or set(normalized_outcomes) != {"yes", "no"}:
        reasons.append("unsupported_outcome_shape")
    if asset and asset not in SUPPORTED_ASSETS:
        reasons.append(f"unsupported_asset:{asset}")
    if not asset:
        reasons.append("unsupported_or_missing_asset")
    if provider and provider != "binance":
        reasons.append(f"unsupported_settlement_provider:{provider}")
    expected_pair = f"{asset}/USDT" if asset else None
    if pair and expected_pair and pair != expected_pair:
        reasons.append(f"pair_mismatch:expected={expected_pair},actual={pair}")
    if interval and interval != "1m":
        reasons.append(f"unsupported_candle_interval:{interval}")
    if price_field and price_field != "close":
        reasons.append(f"unsupported_price_field:{price_field}")
    if timezone and timezone != SETTLEMENT_TIMEZONE:
        reasons.append(f"unsupported_timezone:{timezone}")
    if observation and observation != "12:00:00":
        reasons.append(f"unsupported_observation_time:{observation}")
    if operator and operator not in {">", "<", ">=", "<="}:
        reasons.append(f"unsupported_operator:{operator}")

    if target_date is not None and not explicit_year and target_time is not None:
        if target_time <= now:
            reasons.append("date_without_year_already_passed")
    if target_time is not None and target_time <= now:
        reasons.append("target_time_not_future")
    if market.gamma_end_date is not None and market.gamma_end_date <= now:
        reasons.append("gamma_market_expired")
    if target_time is not None and market.gamma_end_date is not None:
        mismatch = abs((target_time - market.gamma_end_date).total_seconds())
        if mismatch > 60:
            reasons.append("gamma_end_date_mismatch")

    if market.active is None:
        reasons.append("market_active_status_unknown")
    elif not market.active:
        reasons.append("market_inactive")
    if market.closed is None:
        reasons.append("market_closed_status_unknown")
    elif market.closed:
        reasons.append("market_closed")
    if market.accepting_orders is None:
        reasons.append("market_accepting_orders_status_unknown")
    elif market.accepting_orders is False:
        reasons.append("market_not_accepting_orders")
    if market.enable_order_book is None:
        reasons.append("market_order_book_status_unknown")
    elif market.enable_order_book is False:
        reasons.append("market_order_book_disabled")

    reasons = list(dict.fromkeys(reasons))
    tradable = not reasons
    return CryptoResolutionRule(
        event_id=market.event_id,
        condition_id=market.condition_id,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        asset=asset,
        settlement_provider=provider,
        pair=pair,
        exact_operator=operator or "",
        strike=strike,
        candle_interval=interval,
        price_field=price_field,
        timezone=timezone,
        observation_time=observation,
        target_time_utc=target_time,
        gamma_end_date=market.gamma_end_date,
        parser_version=PARSER_VERSION,
        raw_description=description,
        question=question,
        rule_confidence=1.0 if tradable else 0.0,
        tradable=tradable,
        preview_only=not tradable,
        rejection_reasons=tuple(reasons),
        contract_family=DAILY_THRESHOLD_FAMILY,
        boundary_type="fixed_strike",
        affirmative_outcome="Yes",
        negative_outcome="No",
        series_slug=market.series_slug,
    )


def _parse_short_updown_contract(
    market: CryptoMarket,
    *,
    now: datetime,
) -> CryptoResolutionRule:
    question = market.question.strip()
    description = (market.description or "").strip()
    asset = _detect_asset(question) or _detect_asset(description) or ""
    provider = _extract_provider(description)
    pair = _extract_pair(description)
    start = market.event_start_time
    end = market.gamma_end_date
    interval = _short_interval(market.series_slug, start, end)
    reasons: list[str] = []
    required = {
        "event_id": market.event_id,
        "condition_id": market.condition_id,
        "up_token_id": market.yes_token_id,
        "down_token_id": market.no_token_id,
        "asset": asset,
        "settlement_provider": provider,
        "pair": pair,
        "window_start_time_utc": start,
        "target_time_utc": end,
        "gamma_end_date": market.gamma_end_date,
        "raw_description": description,
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        reasons.append(f"missing_contract_fields:{','.join(missing)}")

    outcomes = tuple(outcome.strip().lower() for outcome in market.outcomes)
    if len(outcomes) != 2 or outcomes != ("up", "down"):
        reasons.append("unsupported_outcome_shape")
    if asset not in SHORT_UPDOWN_ASSETS:
        reasons.append(
            f"unsupported_asset:{asset}" if asset else "unsupported_or_missing_asset"
        )
    if provider and provider != "chainlink":
        reasons.append(f"unsupported_settlement_provider:{provider}")
    expected_pair = (
        asset_contract(asset).chainlink_pair
        if asset in SHORT_UPDOWN_ASSETS
        else None
    )
    if pair and expected_pair and pair != expected_pair:
        reasons.append(f"pair_mismatch:expected={expected_pair},actual={pair}")
    if interval not in {"5m", "15m"}:
        reasons.append(f"unsupported_window_interval:{interval or 'missing'}")
    if not re.search(
        r"end of the time range.*greater than or equal to.*beginning of that range",
        description,
        re.I | re.S,
    ):
        reasons.append("unsupported_updown_boundary_rule")
    if not re.search(r"otherwise.*resolve to [\"']?down", description, re.I | re.S):
        reasons.append("missing_down_resolution_rule")
    if start is not None and end is not None:
        start = _as_utc(start)
        end = _as_utc(end)
        expected_seconds = 300 if interval == "5m" else 900 if interval == "15m" else None
        if start >= end:
            reasons.append("window_time_not_increasing")
        elif expected_seconds is not None and (end - start).total_seconds() != expected_seconds:
            reasons.append("window_duration_mismatch")
    if end is not None and _as_utc(end) <= now:
        reasons.append("target_time_not_future")
        reasons.append("gamma_market_expired")
    reasons.extend(_market_status_reasons(market))
    reasons = list(dict.fromkeys(reasons))
    tradable = not reasons
    return CryptoResolutionRule(
        event_id=market.event_id,
        condition_id=market.condition_id,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        asset=asset,
        settlement_provider=provider,
        pair=pair,
        exact_operator=">=",
        strike=Decimal("0"),
        candle_interval=interval,
        price_field="data_stream_value",
        timezone="UTC",
        observation_time="window_start",
        target_time_utc=_as_utc(end) if end is not None else None,
        gamma_end_date=_as_utc(end) if end is not None else None,
        parser_version=PARSER_VERSION,
        raw_description=description,
        question=question,
        rule_confidence=1.0 if tradable else 0.0,
        tradable=tradable,
        preview_only=not tradable,
        rejection_reasons=tuple(reasons),
        contract_family=SHORT_UPDOWN_FAMILY,
        boundary_type="window_start_price",
        window_start_time_utc=_as_utc(start) if start is not None else None,
        affirmative_outcome="Up",
        negative_outcome="Down",
        series_slug=market.series_slug,
    )


def parse_resolution_rule(
    question: str,
    description: str | None = None,
    *,
    now: datetime | None = None,
) -> CryptoResolutionRule:
    """Compatibility preview parser without authoritative Gamma identifiers.

    A free-text question can be inspected, but it can never become tradable
    because event, condition, token, and Gamma deadline fields are absent.
    """
    received_at = _as_utc(now or datetime.now(UTC))
    market = CryptoMarket(
        market_id="preview",
        event_id=None,
        condition_id=None,
        question=question,
        slug=None,
        description=description,
        active=True,
        closed=False,
        accepting_orders=None,
        enable_order_book=None,
        gamma_end_date=None,
        outcomes=(),
        yes_token_id=None,
        no_token_id=None,
        received_at=received_at,
        raw_payload={"question": question, "description": description},
    )
    return parse_contract(market, now=received_at)


def threshold_satisfied(price: Decimal, strike: Decimal, operator: str) -> bool:
    """Apply exact settlement boundary semantics."""
    if operator == ">":
        return price > strike
    if operator == "<":
        return price < strike
    if operator == ">=":
        return price >= strike
    if operator == "<=":
        return price <= strike
    raise ValueError(f"unsupported operator: {operator}")


def _detect_asset(text: str) -> str | None:
    for alias, symbol in ASSET_ALIASES.items():
        if re.search(rf"\b{alias}\b", text, re.I):
            return symbol
    unsupported = re.search(
        r"\b(DOGE|DOGECOIN|BNB|ADA|CARDANO|AVAX|AVALANCHE|SUI|LINK|CHAINLINK)\b",
        text,
        re.I,
    )
    return unsupported.group(1).upper() if unsupported else None


def _detect_operator(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\b(at least|equal to or greater|greater than or equal)\b", lowered):
        return ">="
    if re.search(r"\b(at most|equal to or less|less than or equal)\b", lowered):
        return "<="
    if re.search(r"\b(above|over|higher than|greater than|exceed|surpass)\b", lowered):
        return ">"
    if re.search(r"\b(below|under|lower than|less than|beneath)\b", lowered):
        return "<"
    return None


def _extract_threshold(text: str) -> Decimal | None:
    match = re.search(
        r"(?:\$\s*([\d,]+(?:\.\d+)?)([kKmM]?)\b)"
        r"|(?:\b([\d,]+(?:\.\d+)?)([kKmM])\b)",
        text,
    )
    if not match:
        return None
    raw = (match.group(1) or match.group(3)).replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    suffix = (match.group(2) or match.group(4) or "").lower()
    if suffix == "k":
        value *= Decimal("1000")
    elif suffix == "m":
        value *= Decimal("1000000")
    return value


def _extract_provider(text: str) -> str | None:
    for name in ("binance", "coinbase", "chainlink", "kraken"):
        if re.search(rf"\b{name}\b", text, re.I):
            return name
    return None


def _extract_pair(text: str) -> str | None:
    assets = "|".join(ASSET_CONTRACTS)
    match = re.search(
        rf"\b({assets})\s*(?:/|-)?\s*(USDT|USD)\b",
        text,
        re.I,
    )
    if not match:
        return None
    return f"{match.group(1).upper()}/{match.group(2).upper()}"


def _extract_candle_interval(text: str) -> str | None:
    if re.search(r"\b(1m|1[- ]minute|one[- ]minute)\b", text, re.I):
        return "1m"
    if re.search(r"\b(1h|1[- ]hour|one[- ]hour)\b", text, re.I):
        return "1h"
    return None


def _short_interval(
    series_slug: str | None,
    start: datetime | None,
    end: datetime | None,
) -> str | None:
    match = re.search(r"-(5m|15m)$", series_slug or "", re.I)
    if match:
        return match.group(1).lower()
    if start is None or end is None:
        return None
    seconds = (_as_utc(end) - _as_utc(start)).total_seconds()
    return "5m" if seconds == 300 else "15m" if seconds == 900 else None


def _market_status_reasons(market: CryptoMarket) -> list[str]:
    reasons: list[str] = []
    if market.active is None:
        reasons.append("market_active_status_unknown")
    elif not market.active:
        reasons.append("market_inactive")
    if market.closed is None:
        reasons.append("market_closed_status_unknown")
    elif market.closed:
        reasons.append("market_closed")
    if market.accepting_orders is None:
        reasons.append("market_accepting_orders_status_unknown")
    elif market.accepting_orders is False:
        reasons.append("market_not_accepting_orders")
    if market.enable_order_book is None:
        reasons.append("market_order_book_status_unknown")
    elif market.enable_order_book is False:
        reasons.append("market_order_book_disabled")
    return reasons


def _extract_price_field(text: str) -> str | None:
    if re.search(r"\b(close|closing price|closed price)\b", text, re.I):
        return "close"
    if re.search(r"\b(high|highest price)\b", text, re.I):
        return "high"
    if re.search(r"\b(low|lowest price)\b", text, re.I):
        return "low"
    return None


def _extract_timezone(text: str) -> str | None:
    if re.search(r"\b(ET|EST|EDT|Eastern Time)\b|America/New_York", text, re.I):
        return SETTLEMENT_TIMEZONE
    if re.search(r"\bUTC\b", text, re.I):
        return "UTC"
    return None


def _extract_observation_time(text: str) -> str | None:
    if re.search(r"\bnoon\b", text, re.I):
        return "12:00:00"
    match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*"
        r"(?:ET|EST|EDT|Eastern Time)\b",
        text,
        re.I,
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    marker = (match.group(3) or "").lower().replace(".", "")
    if marker == "pm" and hour != 12:
        hour += 12
    if marker == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}:00"


def _extract_date(text: str, *, now: datetime) -> tuple[date | None, bool]:
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    match = re.search(
        rf"\b({month_names})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b",
        text,
        re.I,
    )
    if not match:
        return None, False
    year_text = match.group(3)
    year = int(year_text) if year_text else now.year
    try:
        return date(year, MONTHS[match.group(1).lower()], int(match.group(2))), bool(year_text)
    except ValueError:
        return None, bool(year_text)


def _target_time_utc(
    target_date: date | None,
    observation_time: str | None,
    timezone: str | None,
) -> datetime | None:
    if target_date is None or observation_time is None or timezone is None:
        return None
    parsed_time = time.fromisoformat(observation_time)
    local = datetime.combine(target_date, parsed_time, tzinfo=ZoneInfo(timezone))
    return local.astimezone(UTC)


def _has_path_dependent_language(text: str) -> bool:
    return bool(re.search(r"\b(hit|touch|reach|dip|breach)\b", text, re.I))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
