"""Acceptance tests for the authoritative contract parser."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from crypto_threshold.adapters.polymarket.translator import translate_market
from crypto_threshold.domain.rules import parse_contract, threshold_satisfied
from tests.conftest import NOW, make_market_payload


def _parse(**overrides: object):
    payload = make_market_payload(**overrides)
    return parse_contract(translate_market(payload, received_at=NOW), now=NOW)


def test_complete_supported_contract_is_tradable() -> None:
    rule = _parse()
    assert rule.tradable is True
    assert rule.preview_only is False
    assert (
        rule.event_id,
        rule.condition_id,
        rule.yes_token_id,
        rule.no_token_id,
    ) == ("event-1", "condition-1", "yes-token", "no-token")
    assert (rule.asset, rule.settlement_provider, rule.pair) == (
        "BTC",
        "binance",
        "BTC/USDT",
    )
    assert (rule.exact_operator, rule.strike) == (">", Decimal("100000"))
    assert (rule.candle_interval, rule.price_field) == ("1m", "close")
    assert (rule.timezone, rule.observation_time) == (
        "America/New_York",
        "12:00:00",
    )
    assert rule.raw_description


def test_noon_et_converts_across_dst_and_standard_time() -> None:
    dst = _parse()
    standard = _parse(
        question="Will Bitcoin be above $100,000 on November 15, 2026?",
        endDate="2026-11-15T17:00:00Z",
    )
    assert dst.target_time_utc == datetime(2026, 7, 23, 16, tzinfo=UTC)
    assert standard.target_time_utc == datetime(2026, 11, 15, 17, tzinfo=UTC)


def test_past_yearless_date_is_hard_rejected() -> None:
    rule = _parse(
        question="Will Bitcoin be above $100,000 on June 30?",
        endDate="2026-06-30T16:00:00Z",
    )
    assert rule.tradable is False
    assert "date_without_year_already_passed" in rule.rejection_reasons
    assert "target_time_not_future" in rule.rejection_reasons


def test_expired_gamma_market_is_hard_rejected() -> None:
    rule = _parse(
        question="Will Bitcoin be above $100,000 on July 21, 2026?",
        endDate="2026-07-21T16:00:00Z",
    )
    assert "gamma_market_expired" in rule.rejection_reasons
    assert rule.preview_only is True


def test_source_pair_candle_and_field_mismatches_reject() -> None:
    descriptions = (
        "Coinbase BTC/USDT 1-minute candle Close price at 12:00 PM ET.",
        "Binance BTC/USD 1-minute candle Close price at 12:00 PM ET.",
        "Binance BTC/USDT 1-hour candle Close price at 12:00 PM ET.",
        "Binance BTC/USDT 1-minute candle High price at 12:00 PM ET.",
    )
    expected = (
        "unsupported_settlement_provider",
        "pair_mismatch",
        "unsupported_candle_interval",
        "unsupported_price_field",
    )
    for description, reason in zip(descriptions, expected):
        rule = _parse(description=description)
        assert any(item.startswith(reason) for item in rule.rejection_reasons)


def test_missing_binding_field_is_preview_only() -> None:
    rule = _parse(description="Binance BTC/USDT Close price at 12:00 PM ET.")
    assert rule.tradable is False
    assert rule.preview_only is True
    assert "candle_interval" in (rule.rejection_reason or "")


def test_unknown_market_status_is_fail_closed() -> None:
    payload = make_market_payload()
    for field in ("active", "closed", "acceptingOrders", "enableOrderBook"):
        payload.pop(field)
    rule = parse_contract(translate_market(payload, received_at=NOW), now=NOW)
    assert rule.tradable is False
    assert {
        "market_active_status_unknown",
        "market_closed_status_unknown",
        "market_accepting_orders_status_unknown",
        "market_order_book_status_unknown",
    }.issubset(rule.rejection_reasons)


def test_non_binary_outcomes_are_rejected() -> None:
    rule = _parse(outcomes='["Yes", "No", "Maybe"]', clobTokenIds='["y", "n", "m"]')
    assert "unsupported_outcome_shape" in rule.rejection_reasons


def test_date_number_is_not_misread_as_threshold() -> None:
    rule = _parse(question="Will Bitcoin be above something on July 23, 2026?")
    assert rule.strike == 0
    assert "strike" in (rule.rejection_reason or "")


def test_strict_operator_boundary_is_preserved() -> None:
    strike = Decimal("100")
    assert threshold_satisfied(strike, strike, ">") is False
    assert threshold_satisfied(strike, strike, "<") is False
    assert threshold_satisfied(strike, strike, ">=") is True
    assert threshold_satisfied(strike, strike, "<=") is True
