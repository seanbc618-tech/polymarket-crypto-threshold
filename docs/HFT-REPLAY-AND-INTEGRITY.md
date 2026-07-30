# R1/R2 Microstructure Replay and Research-Integrity Gates

This package implements the first safe, offline slice of the approved R1 and
R2 roadmap. It is deliberately internal and dependency-light: neither
`hftbacktest` nor `freqtrade` is imported, and there is no authenticated venue
client, signer, order submission, cancellation, or reconciliation path.

Implementation is not evidence of profitability. R1 still needs real,
replayable Level-2/trade tapes across independent dates and assets. R2 still
needs a sealed run against the exact candidate dataset and feature pipeline
before any factor can be promoted.

## R1: HFTBacktest-inspired Level-2 replay

Reference semantics:

- [HftBacktest overview](https://hftbacktest.readthedocs.io/en/latest/)
- [latency models](https://hftbacktest.readthedocs.io/en/latest/latency_models.html)
- [order fill and queue models](https://hftbacktest.readthedocs.io/en/latest/order_fill.html)

The local replay implementation is
`crypto_threshold.services.hft_replay_service.HftReplayService`. Its event
contract preserves:

- exchange timestamp and local receipt timestamp separately;
- ordered Level-2 snapshots, absolute depth updates, and aggressor trades;
- constant order-entry and response latency profiles;
- market and limit orders with depth walking;
- all-or-nothing and partial-fill sensitivity;
- risk-averse, identity-probability, and square-probability queue models;
- same-price queue consumption before a resting fill;
- deterministic manifest hashing;
- execution attribution split into spread capture, directional inventory, and
  fees;
- a sensitivity flag when a positive marked result exists only under a less
  conservative fill or queue assumption.

The risk-averse model is the promotion reference. Probability queue models are
sensitivity cases, not permission to choose whichever simulation looks best.
Insufficient market depth fails closed under the all-or-nothing model instead
of inventing liquidity.

The feature extractor records top-N depth imbalance, microprice, VAMP, recent
aggressive-trade imbalance, spread, and observed feed latency. The independent
`microstructure-shadow` collector additionally records a Binance USD-M
mark/index basis and a bucketed BTC-to-altcoin lead correlation when enough
synchronized samples exist. Missing synchronized data is stored as `NULL`,
never imputed as an edge.

Example:

```python
from crypto_threshold.domain.microstructure import (
    FillModel,
    LatencyProfile,
    QueueModel,
)
from crypto_threshold.services.hft_replay_service import HftReplayService

result = HftReplayService().replay(
    order,
    events,
    latency=LatencyProfile(name="50ms", entry_ms=50, response_ms=50),
    queue_model=QueueModel.RISK_AVERSE,
    fill_model=FillModel.ALL_OR_NOTHING,
)
```

`events` must begin with a complete, uncrossed snapshot. Event IDs must be
unique; exchange times and sequences must increase; every event must carry a
source version and SHA-256 payload hash; receipt time cannot precede exchange
time; and an order cannot cite a decision event that was not yet received
locally. Each replay order also carries the exact strategy version.

## R2: Freqtrade-inspired anti-cheating gates

Reference semantics:

- [Freqtrade lookahead analysis](https://docs.freqtrade.io/en/latest/lookahead-analysis/)
- [Freqtrade recursive analysis](https://docs.freqtrade.io/en/stable/recursive-analysis/)

The local implementation is
`crypto_threshold.services.research_integrity_service.ResearchIntegrityService`.
It performs:

1. **Raw-input timestamp audit.** Every source has observed and received
   timestamps. Both must be at or before the decision. Target-only payloads,
   including settlement labels, are forbidden feature inputs.
2. **Prefix lookahead analysis.** Build the complete baseline, then rebuild
   every chronological prefix. The last value of each prefix must match the
   baseline value for that same row within the declared tolerance.
3. **Startup/recursive analysis.** Rebuild the final feature vector from
   multiple declared startup-window lengths and record per-feature relative
   variance.
4. **Gap and shape checks.** Duplicate rows/sources, non-chronological input,
   missing hashes, non-finite values, malformed feature output, and optional
   timestamp gaps fail closed.
5. **Grouped chronological holdout.** All strikes or rows in one
   `(asset, target_time_utc)` event stay together. Explicit purge and embargo
   intervals exclude overlapping feature/label windows.
6. **Deterministic sealing.** Inputs, settings, computed baseline, violations,
   recursive variances, and split membership are hashed into reproducible
   manifests.
7. **Dry-run isolation.** Trading-disabled state, credential absence,
   authenticated-channel disablement, and mutation-surface absence are checked
   explicitly and sealed in a separate isolation manifest.

Feature builders receive an immutable tuple of chronological `ResearchRow`
objects and must return one `FeatureVector` for every row in the same order.
The API intentionally reruns the builder instead of inspecting source code;
this catches behavior that changes when future rows or startup history are
removed. Callers must declare a non-empty feature-builder version, at least two
startup windows, and a positive maximum timestamp gap; these values are part of
the sealed manifest.

The shadow runner executes this gate separately for BTC, ETH, SOL, and XRP so
one asset's history cannot satisfy another asset's startup window. It retains
the look-ahead/recursive report once 102 rows exist even when the chronological
split is still collecting. The deployed five-minute research horizon declares
a 600-second purge and 300-second embargo; a split becomes `passed` only when
both train and test event-group partitions remain non-empty after those
exclusions.

## Acceptance boundary

The current implementation is a core engineering milestone only. The public
capture path is `BinanceMicrostructureStream` plus
`BinanceMicrostructureRestClient`, and its SQLite store is intentionally
separate from both existing Polymarket shadow databases.

The collector does not fetch a REST snapshot until every configured symbol has
produced at least one buffered WebSocket event. A reconnect clears that
per-generation readiness set and forces a new snapshot bridge. Missing update
IDs are retained as an explicit rejection; they are never interpolated.

R1 is not accepted as strategy evidence until real L2/trade tapes can be
replayed, the same candidate remains viable across at least two latency and
fill assumptions, and its marked edge does not depend on an optimistic queue
model. The collector being deployed only establishes an auditable data path.

R2 is not accepted until the exact candidate dataset and feature builder
produce:

- zero future observation/receipt violations;
- zero target-only input violations;
- zero unexplained lookahead mismatches;
- declared and reviewed recursive tolerances;
- a non-empty purged/embargoed event-group holdout; and
- a reproducible sealed manifest.

Neither acceptance authorizes live trading. `TRADING_DISABLED=true`, the
absence of a signing route, and the existing live NO-GO remain unchanged.
