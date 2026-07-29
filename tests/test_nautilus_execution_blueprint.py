"""Acceptance tests for the isolated Nautilus-inspired execution blueprint."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_threshold.domain.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionOrderIntent,
    ExecutionOrderSide,
    ExecutionOrderStatus,
    ExecutionOrderType,
    ExecutionQuantityUnit,
    ExecutionTimeInForce,
)
from crypto_threshold.services.nautilus_execution_blueprint import (
    NAUTILUS_REFERENCE_COMMIT,
    NAUTILUS_REFERENCE_TAG,
    DisabledExecutionMutationPort,
    ExecutionBlueprintError,
    ExecutionBlueprintRegistry,
    ExecutionMutationDisabled,
    NautilusExecutionBlueprint,
)

NOW = datetime(2026, 7, 30, 0, 0, tzinfo=UTC)


def _intent(**updates: object) -> ExecutionOrderIntent:
    values: dict[str, object] = {
        "client_order_id": "client-1",
        "strategy_id": "cex-kline-v4",
        "signal_id": "signal-1",
        "market_id": "market-1",
        "token_id": "token-up",
        "outcome": "Up",
        "order_type": ExecutionOrderType.MARKET,
        "side": ExecutionOrderSide.BUY,
        "time_in_force": ExecutionTimeInForce.IOC,
        "quantity": Decimal("10"),
        "quantity_unit": ExecutionQuantityUnit.QUOTE_NOTIONAL,
        "price": Decimal("0.55"),
        "created_at": NOW,
    }
    values.update(updates)
    return ExecutionOrderIntent(**values)  # type: ignore[arg-type]


def _event(
    kind: ExecutionEventKind,
    *,
    event_id: str,
    **updates: object,
) -> ExecutionEvent:
    values: dict[str, object] = {
        "event_id": event_id,
        "client_order_id": "client-1",
        "kind": kind,
        "occurred_at": NOW,
        "received_at": NOW,
    }
    values.update(updates)
    return ExecutionEvent(**values)  # type: ignore[arg-type]


def test_market_buy_maps_quote_notional_and_ioc_to_fak() -> None:
    blueprint = NautilusExecutionBlueprint()
    plan = blueprint.plan_order(_intent(), now=NOW)

    assert plan.polymarket_order_type == "FAK"
    assert plan.quantity_unit is ExecutionQuantityUnit.QUOTE_NOTIONAL
    assert plan.price == Decimal("0.55")
    assert plan.requires_signature is True
    assert plan.submission_enabled is False
    assert plan.reference_tag == NAUTILUS_REFERENCE_TAG
    assert plan.reference_commit == NAUTILUS_REFERENCE_COMMIT


def test_market_buy_token_quantity_is_denied_before_any_adapter() -> None:
    with pytest.raises(
        ExecutionBlueprintError,
        match="market_buy_requires_quote_notional",
    ):
        NautilusExecutionBlueprint().plan_order(
            _intent(quantity_unit=ExecutionQuantityUnit.TOKEN),
            now=NOW,
        )


def test_market_sell_requires_token_quantity_and_maps_fok() -> None:
    plan = NautilusExecutionBlueprint().plan_order(
        _intent(
            side=ExecutionOrderSide.SELL,
            quantity=Decimal("10"),
            quantity_unit=ExecutionQuantityUnit.TOKEN,
            time_in_force=ExecutionTimeInForce.FOK,
        ),
        now=NOW,
    )

    assert plan.polymarket_order_type == "FOK"
    assert plan.quantity_unit is ExecutionQuantityUnit.TOKEN


def test_market_sell_quote_quantity_is_denied() -> None:
    with pytest.raises(
        ExecutionBlueprintError,
        match="market_sell_requires_token_quantity",
    ):
        NautilusExecutionBlueprint().plan_order(
            _intent(side=ExecutionOrderSide.SELL),
            now=NOW,
        )


def test_resting_limit_is_token_quantity_and_post_only_compatible() -> None:
    plan = NautilusExecutionBlueprint().plan_order(
        _intent(
            order_type=ExecutionOrderType.LIMIT,
            quantity=Decimal("5"),
            quantity_unit=ExecutionQuantityUnit.TOKEN,
            time_in_force=ExecutionTimeInForce.GTC,
            post_only=True,
        ),
        now=NOW,
    )

    assert plan.polymarket_order_type == "GTC"
    assert plan.post_only is True
    assert plan.batch_eligible is True


def test_post_only_and_market_tif_is_denied() -> None:
    with pytest.raises(
        ExecutionBlueprintError,
        match="post_only_requires_limit_gtc_or_gtd",
    ):
        NautilusExecutionBlueprint().plan_order(
            _intent(
                order_type=ExecutionOrderType.LIMIT,
                quantity=Decimal("5"),
                quantity_unit=ExecutionQuantityUnit.TOKEN,
                post_only=True,
            ),
            now=NOW,
        )


def test_gtd_requires_conservative_expiration_lead() -> None:
    blueprint = NautilusExecutionBlueprint()
    too_soon = _intent(
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("5"),
        quantity_unit=ExecutionQuantityUnit.TOKEN,
        time_in_force=ExecutionTimeInForce.GTD,
        expire_at=NOW + timedelta(seconds=179),
    )
    with pytest.raises(
        ExecutionBlueprintError,
        match="gtd_expiry_requires_180_second_lead",
    ):
        blueprint.plan_order(too_soon, now=NOW)

    plan = blueprint.plan_order(
        replace(too_soon, expire_at=NOW + timedelta(seconds=180)),
        now=NOW,
    )
    assert plan.expiration_unix == int((NOW + timedelta(seconds=180)).timestamp())


def test_venue_minimums_fail_closed() -> None:
    blueprint = NautilusExecutionBlueprint()
    with pytest.raises(
        ExecutionBlueprintError,
        match="marketable_notional_below_1_pusd",
    ):
        blueprint.plan_order(_intent(quantity=Decimal("0.99")), now=NOW)

    with pytest.raises(
        ExecutionBlueprintError,
        match="resting_quantity_below_5_tokens",
    ):
        blueprint.plan_order(
            _intent(
                order_type=ExecutionOrderType.LIMIT,
                quantity=Decimal("4.999"),
                quantity_unit=ExecutionQuantityUnit.TOKEN,
                time_in_force=ExecutionTimeInForce.GTC,
            ),
            now=NOW,
        )


def test_batch_is_limit_only_unique_and_capped_at_fifteen() -> None:
    blueprint = NautilusExecutionBlueprint()
    limit = _intent(
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("5"),
        quantity_unit=ExecutionQuantityUnit.TOKEN,
        time_in_force=ExecutionTimeInForce.GTC,
    )
    plans = blueprint.plan_batch(
        tuple(replace(limit, client_order_id=f"client-{index}") for index in range(15)),
        now=NOW,
    )
    assert len(plans) == 15

    with pytest.raises(ExecutionBlueprintError, match="limit_15"):
        blueprint.plan_batch(
            tuple(
                replace(limit, client_order_id=f"client-{index}")
                for index in range(16)
            ),
            now=NOW,
        )
    with pytest.raises(ExecutionBlueprintError, match="independent_limit"):
        blueprint.plan_batch((_intent(),), now=NOW)


def test_ambiguous_submit_stays_submitted_and_defers_cancel() -> None:
    blueprint = NautilusExecutionBlueprint()
    intent = _intent()
    state = blueprint.initial_state(intent)
    state = blueprint.apply_event(
        state,
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1"),
    )
    state = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.SUBMIT_UNKNOWN,
            event_id="event-2",
            expected_venue_order_id="0xexpected",
            reason="transport_timeout",
        ),
    )

    assert state.status is ExecutionOrderStatus.SUBMITTED
    assert state.reconciliation_required is True
    assert state.expected_venue_order_id == "0xexpected"

    state = blueprint.apply_event(
        state,
        _event(ExecutionEventKind.CANCEL_REQUESTED, event_id="event-3"),
    )
    assert state.status is ExecutionOrderStatus.SUBMITTED
    assert state.cancel_requested is True
    assert state.deferred_cancel_ready is False

    state = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.ACCEPTED,
            event_id="event-4",
            venue_order_id="0xexpected",
            reconciliation=True,
        ),
    )
    assert state.status is ExecutionOrderStatus.PENDING_CANCEL
    assert state.deferred_cancel_ready is True
    assert state.reconciliation_event_count == 1


def test_expected_order_hash_conflict_fails_closed() -> None:
    blueprint = NautilusExecutionBlueprint()
    state = blueprint.initial_state(_intent())
    state = blueprint.apply_event(
        state,
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1"),
    )
    state = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.SUBMIT_UNKNOWN,
            event_id="event-2",
            expected_venue_order_id="0xexpected",
        ),
    )

    with pytest.raises(ExecutionBlueprintError, match="conflicts_with_expected"):
        blueprint.apply_event(
            state,
            _event(
                ExecutionEventKind.ACCEPTED,
                event_id="event-3",
                venue_order_id="0xother",
            ),
        )


def test_expected_order_hash_cannot_change_while_submit_is_unknown() -> None:
    blueprint = NautilusExecutionBlueprint()
    state = blueprint.initial_state(_intent())
    state = blueprint.apply_event(
        state,
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1"),
    )
    state = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.SUBMIT_UNKNOWN,
            event_id="event-2",
            expected_venue_order_id="0xexpected",
        ),
    )
    with pytest.raises(ExecutionBlueprintError, match="expected_venue_order_id_changed"):
        blueprint.apply_event(
            state,
            _event(
                ExecutionEventKind.SUBMIT_UNKNOWN,
                event_id="event-3",
                expected_venue_order_id="0xdifferent",
            ),
        )


def test_fill_events_are_trade_idempotent_across_sources() -> None:
    blueprint = NautilusExecutionBlueprint()
    state = blueprint.initial_state(
        _intent(
            order_type=ExecutionOrderType.LIMIT,
            quantity=Decimal("10"),
            quantity_unit=ExecutionQuantityUnit.TOKEN,
            time_in_force=ExecutionTimeInForce.GTC,
        )
    )
    state = blueprint.apply_event(
        state,
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1"),
    )
    state = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.ACCEPTED,
            event_id="event-2",
            venue_order_id="0xorder",
        ),
    )
    fill = _event(
        ExecutionEventKind.FILLED,
        event_id="event-3",
        venue_order_id="0xorder",
        fill_quantity_tokens=Decimal("4"),
        fill_price=Decimal("0.5"),
        trade_id="trade-1",
    )
    state = blueprint.apply_event(state, fill)
    assert state.status is ExecutionOrderStatus.PARTIALLY_FILLED
    assert state.filled_tokens == Decimal("4")
    assert state.filled_quote_notional == Decimal("2.0")

    state = blueprint.apply_event(
        state,
        replace(fill, event_id="event-4", reconciliation=True),
    )
    assert state.filled_tokens == Decimal("4")
    assert state.reconciliation_event_count == 1


def test_complete_fill_is_terminal_and_overfill_is_rejected() -> None:
    blueprint = NautilusExecutionBlueprint()
    intent = _intent(
        order_type=ExecutionOrderType.LIMIT,
        quantity=Decimal("5"),
        quantity_unit=ExecutionQuantityUnit.TOKEN,
        time_in_force=ExecutionTimeInForce.GTC,
    )
    state = blueprint.initial_state(intent)
    state = blueprint.apply_event(
        state,
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1"),
    )

    with pytest.raises(ExecutionBlueprintError, match="exceeds_requested"):
        blueprint.apply_event(
            state,
            _event(
                ExecutionEventKind.FILLED,
                event_id="event-2",
                fill_quantity_tokens=Decimal("6"),
                fill_price=Decimal("0.5"),
                trade_id="trade-overfill",
                fill_complete=True,
            ),
        )

    state = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.FILLED,
            event_id="event-3",
            fill_quantity_tokens=Decimal("5"),
            fill_price=Decimal("0.5"),
            trade_id="trade-complete",
            fill_complete=True,
        ),
    )
    assert state.status is ExecutionOrderStatus.FILLED
    assert state.terminal is True

    duplicate = blueprint.apply_event(
        state,
        _event(
            ExecutionEventKind.FILLED,
            event_id="event-4",
            fill_quantity_tokens=Decimal("5"),
            fill_price=Decimal("0.5"),
            trade_id="trade-complete",
            fill_complete=True,
            reconciliation=True,
        ),
    )
    assert duplicate.filled_tokens == Decimal("5")
    assert duplicate.reconciliation_event_count == 1


def test_manifest_is_reference_only_and_pinned_to_peeled_commit() -> None:
    manifest = NautilusExecutionBlueprint().manifest()
    assert manifest["status"] == "reference_blueprint_only"
    assert manifest["live_submission"] is False
    assert manifest["credentials"] is False
    assert manifest["nautilus_reference_tag"] == "v1.230.0"
    assert manifest["nautilus_reference_commit"] == (
        "8160730c7c550480b0a439fb11086a4c4de15f0b"
    )
    assert manifest["tif_mapping"] == {
        "GTC": "GTC",
        "GTD": "GTD",
        "FOK": "FOK",
        "IOC": "FAK",
    }


def test_mutation_port_is_mechanically_disabled() -> None:
    with pytest.raises(ExecutionMutationDisabled, match="TRADING_DISABLED"):
        DisabledExecutionMutationPort(trading_disabled=False)

    port = DisabledExecutionMutationPort(trading_disabled=True)
    plan = NautilusExecutionBlueprint().plan_order(_intent(), now=NOW)
    with pytest.raises(ExecutionMutationDisabled, match="submit"):
        port.submit_order(plan)
    with pytest.raises(ExecutionMutationDisabled, match="cancel"):
        port.cancel_order("client-1")
    with pytest.raises(ExecutionMutationDisabled, match="reconciliation"):
        port.reconcile_account()


def test_registry_makes_client_order_id_idempotent_and_conflict_safe() -> None:
    registry = ExecutionBlueprintRegistry()
    intent = _intent()
    first = registry.register(intent, now=NOW)
    repeated = registry.register(intent, now=NOW)
    assert repeated is first
    assert registry.state("client-1").status is ExecutionOrderStatus.INITIALIZED

    with pytest.raises(ExecutionBlueprintError, match="reused_for_different_intent"):
        registry.register(replace(intent, quantity=Decimal("11")), now=NOW)

    submitted = registry.apply(
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1")
    )
    assert submitted.status is ExecutionOrderStatus.SUBMITTED
    assert registry.apply(
        _event(ExecutionEventKind.SUBMITTED, event_id="event-1")
    ) is submitted
