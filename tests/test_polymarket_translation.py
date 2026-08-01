"""Gamma token mapping and CLOB executable-price tests."""

from __future__ import annotations

import json
from decimal import Decimal

from crypto_threshold.adapters.polymarket.translator import (
    translate_market,
    translate_order_book,
)
from crypto_threshold.domain.markets import OrderBookLevel, calculate_ask_vwap
from tests.conftest import NOW, make_book, make_market_payload


def test_yes_no_mapping_follows_outcome_order() -> None:
    payload = make_market_payload(
        outcomes=json.dumps(["No", "Yes"]),
        clobTokenIds=json.dumps(["no-token", "yes-token"]),
    )
    market = translate_market(payload, received_at=NOW)
    assert market.yes_token_id == "yes-token"
    assert market.no_token_id == "no-token"


def test_up_down_mapping_follows_outcome_order() -> None:
    payload = make_market_payload(
        outcomes=json.dumps(["Down", "Up"]),
        clobTokenIds=json.dumps(["down-token", "up-token"]),
    )
    market = translate_market(payload, received_at=NOW)
    assert market.yes_token_id == "up-token"
    assert market.no_token_id == "down-token"


def test_malformed_clob_token_ids_do_not_guess_mapping() -> None:
    market = translate_market(
        make_market_payload(clobTokenIds="not-json"), received_at=NOW
    )
    assert market.yes_token_id is None
    assert market.no_token_id is None


def test_executable_ask_vwap_is_not_midpoint() -> None:
    snapshot = translate_order_book(
        market_id="market-1",
        token_id="yes-token",
        outcome="YES",
        payload=make_book(outcome="YES"),
        received_at=NOW,
    )
    execution = calculate_ask_vwap(snapshot.asks, Decimal("10"))
    assert snapshot.midpoint == Decimal("0.395")
    assert execution.complete is True
    assert execution.vwap is not None
    assert execution.vwap > snapshot.best_ask
    assert execution.vwap != snapshot.midpoint


def test_insufficient_depth_is_rejected() -> None:
    snapshot = translate_order_book(
        market_id="market-1",
        token_id="yes-token",
        outcome="YES",
        payload=make_book(outcome="YES"),
        received_at=NOW,
    )
    execution = calculate_ask_vwap(snapshot.asks, Decimal("1000"))
    assert execution.complete is False
    assert execution.reasons == ("insufficient_ask_depth",)


def test_ask_vwap_filters_invalid_depth_and_never_records_negative_slippage() -> None:
    execution = calculate_ask_vwap(
        (
            OrderBookLevel(price=Decimal("0.60"), size=Decimal("10")),
            OrderBookLevel(price=Decimal("0.40"), size=Decimal("10")),
            OrderBookLevel(price=Decimal("0.30"), size=Decimal("-100")),
            OrderBookLevel(price=Decimal("0.20"), size=Decimal("0")),
        ),
        Decimal("10"),
    )

    assert execution.complete is True
    assert execution.best_ask == Decimal("0.40")
    assert execution.vwap is not None
    assert execution.vwap >= execution.best_ask
    assert execution.slippage_per_share is not None
    assert execution.slippage_per_share >= 0
    assert "negative_slippage_invariant" not in execution.reasons
