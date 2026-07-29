# NautilusTrader-Inspired Execution Blueprint

**Status:** isolated reference implementation  
**Live submission:** unavailable  
**Signing or credentials:** unavailable  
**Authenticated reconciliation:** unavailable

## Purpose

This blueprint defines the execution boundary that a future live-capital phase
would have to satisfy. It deliberately does not implement a venue client,
signer, credential loader, order database, or network mutation. Its current
purpose is to make dangerous execution semantics testable before any such
component exists.

The implementation follows NautilusTrader's event-driven order model and its
documented Polymarket translation rules, while remaining a small internal
domain/service layer:

- `src/crypto_threshold/domain/execution.py`
- `src/crypto_threshold/services/nautilus_execution_blueprint.py`
- `tests/test_nautilus_execution_blueprint.py`

Inspect the non-executable capability map with:

```bash
uv run crypto-threshold execution-blueprint
```

## Pinned Reference

The reviewed reference is the NautilusTrader `v1.230.0` release tag:

- release date: `2026-06-29T12:06:45Z`
- annotated tag object:
  `112d335088ec11cdd1d60038b16c8fe56406aead`
- peeled source commit:
  `8160730c7c550480b0a439fb11086a4c4de15f0b`
- integration document:
  <https://github.com/nautechsystems/nautilus_trader/blob/v1.230.0/docs/integrations/polymarket.md>

The release describes itself as beta. The project therefore pins it only as a
reviewable semantic reference: NautilusTrader is not installed, imported, or
approved as a production dependency.

## Order Capability Map

| Internal order | Internal TIF | Polymarket `orderType` | Allowed |
|---|---|---|---|
| Market | IOC | FAK | yes |
| Market | FOK | FOK | yes |
| Market | GTC/GTD | — | denied |
| Limit | GTC | GTC | yes |
| Limit | GTD | GTD | yes |
| Limit | IOC | FAK | yes |
| Limit | FOK | FOK | yes |

`IOC` is the Nautilus name for the semantics Polymarket calls `FAK`.
Post-only is accepted only for Limit GTC/GTD. Reduce-only, modification,
bracket/OCO, and iceberg behavior are denied before any future adapter boundary.
Independent limit-order batches are capped at 15.

## Quantity and Price Contract

| Order | Required quantity unit | Price meaning |
|---|---|---|
| Market BUY | quote notional in pUSD | worst acceptable price |
| Market SELL | outcome-token quantity | worst acceptable price |
| Limit BUY | outcome-token quantity | limit price |
| Limit SELL | outcome-token quantity | limit price |

A Market BUY expressed in tokens is denied because treating it as quote
notional could multiply the intended spend. Marketable FAK/FOK plans require at
least `1 pUSD` notional. Resting GTC/GTD plans require at least five tokens.
Prices must remain strictly between zero and one.

GTD plans require a timezone-aware expiration with at least 180 seconds of lead
time. This local policy is intentionally more conservative than the venue's
roughly one-minute expiration buffer.

## Deterministic Lifecycle

```text
INITIALIZED
  -> DENIED
  -> SUBMITTED
       -> ACCEPTED
       -> REJECTED
       -> PARTIALLY_FILLED
       -> FILLED
       -> CANCELED

ACCEPTED / PARTIALLY_FILLED
  -> PENDING_CANCEL
       -> CANCELED
       -> FILLED
       -> ACCEPTED or PARTIALLY_FILLED on cancel rejection
```

Every event carries a unique event ID. Fills also carry a trade ID, which is
deduplicated independently so a WebSocket fill and a later reconciliation fill
cannot double-count quantity or notional.

`ExecutionBlueprintRegistry` is an in-memory test harness which makes
`client_order_id` idempotent: replaying the identical intent returns the same
plan, while reusing the ID for different intent content fails closed. It is not
a durable order store.

## Ambiguous Submit Outcomes

A timeout, malformed response, transport failure, or unknown retry outcome is
not proof of rejection. The blueprint therefore:

1. keeps the order in `SUBMITTED`;
2. records the expected venue order hash when available;
3. sets `reconciliation_required=true`;
4. defers a requested cancel until the venue order ID is confirmed;
5. rejects a later venue ID that conflicts with the expected hash.

This mirrors the core Nautilus rule: direct events handle the happy path, while
venue reports reconcile races and unknown outcomes.

The current project does not perform that reconciliation. The state machine can
apply synthetic or future report events marked `reconciliation=true`, but
`DisabledExecutionMutationPort.reconcile_account()` always raises
`ExecutionMutationDisabled`.

## Safety Boundary

The current blueprint creates only an unsigned `UnsignedPolymarketOrderPlan`.
Every plan has:

- `requires_signature=true`;
- `submission_enabled=false`;
- a deterministic SHA-256 fingerprint of the complete intent;
- the pinned Nautilus reference tag and commit.

`DisabledExecutionMutationPort` refuses construction when
`TRADING_DISABLED=false`, and its submit, cancel, and authenticated
reconciliation methods always raise. No production service or CLI command
constructs a live execution client.

The existing shadow services, databases, paper ledgers, VPS configuration, and
public REST clients are unchanged.

## Future Promotion Requirements

The blueprint is not permission to implement or activate a live adapter.
Before replacing the mutation lock, all of the following remain required:

- positive event-diverse, fee/slippage-adjusted executable OOS evidence;
- R2 look-ahead/integrity gates;
- conservative R1 fill/queue/latency replay;
- explicit capital, per-order, inventory, daily-loss, and kill limits;
- reviewed credential isolation and signing design;
- durable event storage plus restart recovery;
- authenticated order/fill/position reconciliation tests;
- ambiguous-submit and cancel-race chaos tests;
- a separate owner authorization for live capital.
