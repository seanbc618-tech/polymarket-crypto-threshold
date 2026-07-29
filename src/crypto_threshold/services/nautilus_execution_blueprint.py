"""NautilusTrader-inspired Polymarket execution blueprint with live I/O locked."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NoReturn

from crypto_threshold.domain.execution import (
    ExecutionEvent,
    ExecutionEventKind,
    ExecutionOrderIntent,
    ExecutionOrderSide,
    ExecutionOrderState,
    ExecutionOrderStatus,
    ExecutionOrderType,
    ExecutionQuantityUnit,
    ExecutionTimeInForce,
    UnsignedPolymarketOrderPlan,
)

NAUTILUS_REFERENCE_TAG = "v1.230.0"
NAUTILUS_REFERENCE_TAG_OBJECT = "112d335088ec11cdd1d60038b16c8fe56406aead"
NAUTILUS_REFERENCE_COMMIT = "8160730c7c550480b0a439fb11086a4c4de15f0b"
NAUTILUS_REFERENCE_RELEASED_AT = "2026-06-29T12:06:45Z"
POLYMARKET_BATCH_LIMIT = 15
POLYMARKET_MARKETABLE_MIN_NOTIONAL = Decimal("1")
POLYMARKET_RESTING_MIN_TOKENS = Decimal("5")
POLYMARKET_GTD_MIN_LEAD_SECONDS = 180
TOKEN_FILL_DUST = Decimal("0.000001")

_TIF_TO_POLYMARKET = {
    ExecutionTimeInForce.GTC: "GTC",
    ExecutionTimeInForce.GTD: "GTD",
    ExecutionTimeInForce.FOK: "FOK",
    ExecutionTimeInForce.IOC: "FAK",
}


class ExecutionBlueprintError(ValueError):
    """The intent or event violates the declared future execution contract."""


class ExecutionMutationDisabled(RuntimeError):
    """No authenticated execution mutation is available in this blueprint."""


class NautilusExecutionBlueprint:
    """Validate translation and lifecycle semantics without importing Nautilus."""

    def manifest(self) -> dict[str, object]:
        return {
            "status": "reference_blueprint_only",
            "live_submission": False,
            "credentials": False,
            "nautilus_reference_tag": NAUTILUS_REFERENCE_TAG,
            "nautilus_reference_tag_object": NAUTILUS_REFERENCE_TAG_OBJECT,
            "nautilus_reference_commit": NAUTILUS_REFERENCE_COMMIT,
            "nautilus_reference_released_at": NAUTILUS_REFERENCE_RELEASED_AT,
            "tif_mapping": {
                tif.value: venue for tif, venue in _TIF_TO_POLYMARKET.items()
            },
            "quantity_semantics": {
                "MARKET_BUY": ExecutionQuantityUnit.QUOTE_NOTIONAL.value,
                "MARKET_SELL": ExecutionQuantityUnit.TOKEN.value,
                "LIMIT_BUY": ExecutionQuantityUnit.TOKEN.value,
                "LIMIT_SELL": ExecutionQuantityUnit.TOKEN.value,
            },
            "batch_limit": POLYMARKET_BATCH_LIMIT,
            "unsupported": [
                "modify",
                "reduce_only",
                "bracket",
                "oco",
                "iceberg",
            ],
            "unknown_submit_policy": "remain_submitted_and_reconcile",
        }

    def plan_order(
        self,
        intent: ExecutionOrderIntent,
        *,
        now: datetime | None = None,
    ) -> UnsignedPolymarketOrderPlan:
        current = _utc(now or datetime.now(UTC))
        self._validate_intent(intent, now=current)
        expiration = (
            int(_utc(intent.expire_at).timestamp())
            if intent.expire_at is not None
            else None
        )
        return UnsignedPolymarketOrderPlan(
            client_order_id=intent.client_order_id,
            market_id=intent.market_id,
            token_id=intent.token_id,
            outcome=intent.outcome,
            side=intent.side,
            order_type=intent.order_type,
            nautilus_time_in_force=intent.time_in_force,
            polymarket_order_type=_TIF_TO_POLYMARKET[intent.time_in_force],
            quantity=intent.quantity,
            quantity_unit=intent.quantity_unit,
            price=intent.price,
            expiration_unix=expiration,
            post_only=intent.post_only,
            batch_eligible=(
                intent.order_type is ExecutionOrderType.LIMIT
                and intent.quantity_unit is ExecutionQuantityUnit.TOKEN
            ),
            intent_fingerprint=_intent_fingerprint(intent),
            reference_tag=NAUTILUS_REFERENCE_TAG,
            reference_commit=NAUTILUS_REFERENCE_COMMIT,
        )

    def plan_batch(
        self,
        intents: tuple[ExecutionOrderIntent, ...],
        *,
        now: datetime | None = None,
    ) -> tuple[UnsignedPolymarketOrderPlan, ...]:
        if not intents:
            raise ExecutionBlueprintError("batch_must_not_be_empty")
        if len(intents) > POLYMARKET_BATCH_LIMIT:
            raise ExecutionBlueprintError("batch_exceeds_polymarket_limit_15")
        plans = tuple(self.plan_order(intent, now=now) for intent in intents)
        if any(not plan.batch_eligible for plan in plans):
            raise ExecutionBlueprintError("batch_requires_independent_limit_orders")
        if len({plan.client_order_id for plan in plans}) != len(plans):
            raise ExecutionBlueprintError("batch_client_order_ids_must_be_unique")
        return plans

    def initial_state(self, intent: ExecutionOrderIntent) -> ExecutionOrderState:
        return ExecutionOrderState(
            client_order_id=intent.client_order_id,
            intent_fingerprint=_intent_fingerprint(intent),
            quantity_unit=intent.quantity_unit,
            requested_quantity=intent.quantity,
        )

    def apply_event(
        self,
        state: ExecutionOrderState,
        event: ExecutionEvent,
    ) -> ExecutionOrderState:
        if event.client_order_id != state.client_order_id:
            raise ExecutionBlueprintError("event_client_order_id_mismatch")
        if event.event_id in state.processed_event_ids:
            return state
        _require_aware(event.occurred_at, field="occurred_at")
        _require_aware(event.received_at, field="received_at")

        if (
            event.kind is ExecutionEventKind.FILLED
            and event.trade_id is not None
            and event.trade_id in state.processed_trade_ids
        ):
            return _record_event(state, event)
        if state.terminal:
            raise ExecutionBlueprintError("terminal_order_rejects_new_event")

        next_state = self._transition(state, event)
        return _record_event(next_state, event)

    def _transition(
        self,
        state: ExecutionOrderState,
        event: ExecutionEvent,
    ) -> ExecutionOrderState:
        if event.kind is ExecutionEventKind.DENIED:
            _require_status(state, {ExecutionOrderStatus.INITIALIZED}, event)
            return replace(
                state,
                status=ExecutionOrderStatus.DENIED,
                reasons=_append_reason(state.reasons, event.reason or "order_denied"),
            )
        if event.kind is ExecutionEventKind.SUBMITTED:
            _require_status(state, {ExecutionOrderStatus.INITIALIZED}, event)
            return replace(state, status=ExecutionOrderStatus.SUBMITTED)
        if event.kind is ExecutionEventKind.SUBMIT_UNKNOWN:
            _require_status(state, {ExecutionOrderStatus.SUBMITTED}, event)
            expected_venue_id = _resolved_expected_venue_id(state, event)
            return replace(
                state,
                expected_venue_order_id=expected_venue_id,
                reconciliation_required=True,
                reasons=_append_reason(
                    state.reasons,
                    event.reason or "submit_outcome_unknown",
                ),
            )
        if event.kind is ExecutionEventKind.ACCEPTED:
            _require_status(
                state,
                {
                    ExecutionOrderStatus.SUBMITTED,
                    ExecutionOrderStatus.PENDING_CANCEL,
                },
                event,
            )
            venue_id = _resolved_venue_id(state, event)
            pending_cancel = state.cancel_requested
            return replace(
                state,
                status=(
                    ExecutionOrderStatus.PENDING_CANCEL
                    if pending_cancel
                    else ExecutionOrderStatus.ACCEPTED
                ),
                venue_order_id=venue_id,
                reconciliation_required=False,
                deferred_cancel_ready=bool(pending_cancel and venue_id),
            )
        if event.kind is ExecutionEventKind.REJECTED:
            _require_status(state, {ExecutionOrderStatus.SUBMITTED}, event)
            return replace(
                state,
                status=ExecutionOrderStatus.REJECTED,
                reconciliation_required=False,
                cancel_requested=False,
                deferred_cancel_ready=False,
                reasons=_append_reason(state.reasons, event.reason or "venue_rejected"),
            )
        if event.kind is ExecutionEventKind.CANCEL_REQUESTED:
            _require_status(
                state,
                {
                    ExecutionOrderStatus.SUBMITTED,
                    ExecutionOrderStatus.ACCEPTED,
                    ExecutionOrderStatus.PARTIALLY_FILLED,
                    ExecutionOrderStatus.PENDING_CANCEL,
                },
                event,
            )
            if state.status is ExecutionOrderStatus.SUBMITTED:
                return replace(
                    state,
                    cancel_requested=True,
                    reconciliation_required=True,
                    deferred_cancel_ready=bool(state.venue_order_id),
                )
            return replace(
                state,
                status=ExecutionOrderStatus.PENDING_CANCEL,
                cancel_requested=True,
                deferred_cancel_ready=bool(state.venue_order_id),
            )
        if event.kind is ExecutionEventKind.CANCEL_REJECTED:
            _require_status(state, {ExecutionOrderStatus.PENDING_CANCEL}, event)
            return replace(
                state,
                status=(
                    ExecutionOrderStatus.PARTIALLY_FILLED
                    if state.filled_tokens > 0
                    else ExecutionOrderStatus.ACCEPTED
                ),
                cancel_requested=False,
                deferred_cancel_ready=False,
                reasons=_append_reason(
                    state.reasons,
                    event.reason or "cancel_rejected",
                ),
            )
        if event.kind is ExecutionEventKind.CANCELED:
            _require_status(
                state,
                {
                    ExecutionOrderStatus.SUBMITTED,
                    ExecutionOrderStatus.ACCEPTED,
                    ExecutionOrderStatus.PARTIALLY_FILLED,
                    ExecutionOrderStatus.PENDING_CANCEL,
                },
                event,
            )
            return replace(
                state,
                status=ExecutionOrderStatus.CANCELED,
                venue_order_id=_resolved_venue_id(state, event, required=False),
                reconciliation_required=False,
                cancel_requested=False,
                deferred_cancel_ready=False,
                reasons=_append_reason(
                    state.reasons,
                    event.reason or "venue_canceled",
                ),
            )
        if event.kind is ExecutionEventKind.FILLED:
            return self._apply_fill(state, event)
        raise ExecutionBlueprintError(f"unsupported_execution_event:{event.kind.value}")

    def _apply_fill(
        self,
        state: ExecutionOrderState,
        event: ExecutionEvent,
    ) -> ExecutionOrderState:
        _require_status(
            state,
            {
                ExecutionOrderStatus.SUBMITTED,
                ExecutionOrderStatus.ACCEPTED,
                ExecutionOrderStatus.PARTIALLY_FILLED,
                ExecutionOrderStatus.PENDING_CANCEL,
            },
            event,
        )
        if not event.trade_id:
            raise ExecutionBlueprintError("fill_requires_trade_id")
        if event.fill_quantity_tokens is None or event.fill_quantity_tokens <= 0:
            raise ExecutionBlueprintError("fill_quantity_tokens_must_be_positive")
        if event.fill_price is None or not Decimal("0") < event.fill_price < Decimal("1"):
            raise ExecutionBlueprintError("fill_price_must_be_between_zero_and_one")

        filled_tokens = state.filled_tokens + event.fill_quantity_tokens
        if (
            state.quantity_unit is ExecutionQuantityUnit.TOKEN
            and filled_tokens > state.requested_quantity + TOKEN_FILL_DUST
        ):
            raise ExecutionBlueprintError("fill_exceeds_requested_token_quantity")
        filled_quote = (
            state.filled_quote_notional
            + event.fill_quantity_tokens * event.fill_price
        )
        complete = event.fill_complete
        status = (
            ExecutionOrderStatus.FILLED
            if complete
            else ExecutionOrderStatus.PENDING_CANCEL
            if state.status is ExecutionOrderStatus.PENDING_CANCEL
            else ExecutionOrderStatus.PARTIALLY_FILLED
        )
        return replace(
            state,
            status=status,
            venue_order_id=_resolved_venue_id(state, event, required=False),
            filled_tokens=filled_tokens,
            filled_quote_notional=filled_quote,
            reconciliation_required=(
                False if event.venue_order_id else state.reconciliation_required
            ),
            cancel_requested=False if complete else state.cancel_requested,
            deferred_cancel_ready=False if complete else state.deferred_cancel_ready,
            processed_trade_ids=state.processed_trade_ids | {event.trade_id},
        )

    def _validate_intent(
        self,
        intent: ExecutionOrderIntent,
        *,
        now: datetime,
    ) -> None:
        for field_name, value in (
            ("client_order_id", intent.client_order_id),
            ("strategy_id", intent.strategy_id),
            ("signal_id", intent.signal_id),
            ("market_id", intent.market_id),
            ("token_id", intent.token_id),
            ("outcome", intent.outcome),
        ):
            if not value.strip():
                raise ExecutionBlueprintError(f"{field_name}_must_not_be_empty")
        _require_aware(intent.created_at, field="created_at")
        if intent.quantity <= 0:
            raise ExecutionBlueprintError("quantity_must_be_positive")
        if not Decimal("0") < intent.price < Decimal("1"):
            raise ExecutionBlueprintError("price_must_be_between_zero_and_one")
        if intent.reduce_only:
            raise ExecutionBlueprintError("polymarket_reduce_only_not_supported")

        if intent.order_type is ExecutionOrderType.MARKET:
            if intent.time_in_force not in {
                ExecutionTimeInForce.IOC,
                ExecutionTimeInForce.FOK,
            }:
                raise ExecutionBlueprintError("market_order_requires_ioc_or_fok")
            if intent.post_only:
                raise ExecutionBlueprintError("market_order_cannot_be_post_only")
            expected_unit = (
                ExecutionQuantityUnit.QUOTE_NOTIONAL
                if intent.side is ExecutionOrderSide.BUY
                else ExecutionQuantityUnit.TOKEN
            )
            if intent.quantity_unit is not expected_unit:
                raise ExecutionBlueprintError(
                    "market_buy_requires_quote_notional"
                    if intent.side is ExecutionOrderSide.BUY
                    else "market_sell_requires_token_quantity"
                )
        elif intent.quantity_unit is not ExecutionQuantityUnit.TOKEN:
            raise ExecutionBlueprintError("limit_order_requires_token_quantity")

        if intent.post_only and (
            intent.order_type is not ExecutionOrderType.LIMIT
            or intent.time_in_force
            not in {ExecutionTimeInForce.GTC, ExecutionTimeInForce.GTD}
        ):
            raise ExecutionBlueprintError("post_only_requires_limit_gtc_or_gtd")

        if intent.time_in_force is ExecutionTimeInForce.GTD:
            if intent.order_type is not ExecutionOrderType.LIMIT:
                raise ExecutionBlueprintError("gtd_requires_limit_order")
            if intent.expire_at is None:
                raise ExecutionBlueprintError("gtd_requires_expire_at")
            _require_aware(intent.expire_at, field="expire_at")
            if _utc(intent.expire_at) < now + timedelta(
                seconds=POLYMARKET_GTD_MIN_LEAD_SECONDS
            ):
                raise ExecutionBlueprintError("gtd_expiry_requires_180_second_lead")
        elif intent.expire_at is not None:
            raise ExecutionBlueprintError("expire_at_requires_gtd")

        if intent.time_in_force in {
            ExecutionTimeInForce.IOC,
            ExecutionTimeInForce.FOK,
        }:
            notional = (
                intent.quantity
                if (
                    intent.order_type is ExecutionOrderType.MARKET
                    and intent.side is ExecutionOrderSide.BUY
                    and intent.quantity_unit is ExecutionQuantityUnit.QUOTE_NOTIONAL
                )
                else intent.quantity * intent.price
            )
            if notional < POLYMARKET_MARKETABLE_MIN_NOTIONAL:
                raise ExecutionBlueprintError("marketable_notional_below_1_pusd")
        elif intent.quantity < POLYMARKET_RESTING_MIN_TOKENS:
            raise ExecutionBlueprintError("resting_quantity_below_5_tokens")


class DisabledExecutionMutationPort:
    """Sentinel proving that the blueprint exposes no executable venue client."""

    def __init__(self, *, trading_disabled: bool) -> None:
        if not trading_disabled:
            raise ExecutionMutationDisabled(
                "blueprint_requires_TRADING_DISABLED_true"
            )

    def submit_order(self, _plan: UnsignedPolymarketOrderPlan) -> NoReturn:
        raise ExecutionMutationDisabled("authenticated_submit_is_not_implemented")

    def cancel_order(self, _client_order_id: str) -> NoReturn:
        raise ExecutionMutationDisabled("authenticated_cancel_is_not_implemented")

    def reconcile_account(self) -> NoReturn:
        raise ExecutionMutationDisabled(
            "authenticated_reconciliation_is_not_implemented"
        )


class ExecutionBlueprintRegistry:
    """In-memory idempotency harness for deterministic tests, never production I/O."""

    def __init__(self, blueprint: NautilusExecutionBlueprint | None = None) -> None:
        self.blueprint = blueprint or NautilusExecutionBlueprint()
        self._plans: dict[str, UnsignedPolymarketOrderPlan] = {}
        self._states: dict[str, ExecutionOrderState] = {}

    def register(
        self,
        intent: ExecutionOrderIntent,
        *,
        now: datetime | None = None,
    ) -> UnsignedPolymarketOrderPlan:
        plan = self.blueprint.plan_order(intent, now=now)
        existing = self._plans.get(plan.client_order_id)
        if existing is not None:
            if existing.intent_fingerprint != plan.intent_fingerprint:
                raise ExecutionBlueprintError(
                    "client_order_id_reused_for_different_intent"
                )
            return existing
        self._plans[plan.client_order_id] = plan
        self._states[plan.client_order_id] = self.blueprint.initial_state(intent)
        return plan

    def state(self, client_order_id: str) -> ExecutionOrderState:
        try:
            return self._states[client_order_id]
        except KeyError as exc:
            raise ExecutionBlueprintError("unknown_client_order_id") from exc

    def apply(self, event: ExecutionEvent) -> ExecutionOrderState:
        current = self.state(event.client_order_id)
        updated = self.blueprint.apply_event(current, event)
        self._states[event.client_order_id] = updated
        return updated


def _intent_fingerprint(intent: ExecutionOrderIntent) -> str:
    payload = {
        "client_order_id": intent.client_order_id,
        "strategy_id": intent.strategy_id,
        "signal_id": intent.signal_id,
        "market_id": intent.market_id,
        "token_id": intent.token_id,
        "outcome": intent.outcome,
        "order_type": intent.order_type.value,
        "side": intent.side.value,
        "time_in_force": intent.time_in_force.value,
        "quantity": str(intent.quantity),
        "quantity_unit": intent.quantity_unit.value,
        "price": str(intent.price),
        "created_at": _utc(intent.created_at).isoformat(),
        "post_only": intent.post_only,
        "reduce_only": intent.reduce_only,
        "expire_at": (
            _utc(intent.expire_at).isoformat()
            if intent.expire_at is not None
            else None
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _resolved_venue_id(
    state: ExecutionOrderState,
    event: ExecutionEvent,
    *,
    required: bool = True,
) -> str | None:
    venue_id = event.venue_order_id or state.venue_order_id
    expected = event.expected_venue_order_id or state.expected_venue_order_id
    if venue_id is not None and expected is not None and venue_id != expected:
        raise ExecutionBlueprintError("venue_order_id_conflicts_with_expected_hash")
    if required and venue_id is None:
        raise ExecutionBlueprintError("venue_order_id_required")
    return venue_id


def _resolved_expected_venue_id(
    state: ExecutionOrderState,
    event: ExecutionEvent,
) -> str | None:
    current = state.expected_venue_order_id
    candidate = event.expected_venue_order_id
    if current is not None and candidate is not None and current != candidate:
        raise ExecutionBlueprintError("expected_venue_order_id_changed")
    return candidate or current


def _require_status(
    state: ExecutionOrderState,
    allowed: set[ExecutionOrderStatus],
    event: ExecutionEvent,
) -> None:
    if state.status not in allowed:
        raise ExecutionBlueprintError(
            f"invalid_transition:{state.status.value}->{event.kind.value}"
        )


def _record_event(
    state: ExecutionOrderState,
    event: ExecutionEvent,
) -> ExecutionOrderState:
    return replace(
        state,
        processed_event_ids=state.processed_event_ids | {event.event_id},
        reconciliation_event_count=(
            state.reconciliation_event_count + int(event.reconciliation)
        ),
        last_event_at=_utc(event.received_at),
    )


def _append_reason(reasons: tuple[str, ...], reason: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*reasons, reason)))


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ExecutionBlueprintError(f"{field}_must_be_timezone_aware")


def _utc(value: datetime) -> datetime:
    _require_aware(value, field="datetime")
    return value.astimezone(UTC)
