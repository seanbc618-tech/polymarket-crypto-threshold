# Phase 2 Final Acceptance

**Review date:** 2026-07-29 Asia/Shanghai
**Classification:** Read-only research evidence closure
**Mechanical verdict:** `ACCEPTED`
**Final project verdict:** Phase 2 research pipeline accepted; model edge not demonstrated
**Live status:** **NO-GO**

## Scope

This report closes the Daily BTC/ETH threshold Phase 2 path:

```text
exact public inputs
-> immutable Binance settlement labels
-> frozen 30-label training replay
-> later fixed-holdout OOS replay
-> calibration and baseline metrics
-> continuous REST-fallback shadow evidence
-> mechanical acceptance
```

The 5m/15m Chainlink Up/Down expansion remains a separate research family and
database. It is not used to satisfy the Daily acceptance result.

## Frozen Training Artifact

The training gate first reached `34/30 READY` on 2026-07-28. A WAL-consistent
independent snapshot was created at:

```text
/opt/polymarket-crypto-threshold/backups/phase2-training-20260728/
crypto-threshold-20260728T101754.978295Z.db
```

Evidence:

- Pre-build database SHA-256:
  `3c9e98a2d5f5d966c971210dfcaae9a81d36739311b42f42f16e1f528aa521e7`
- Frozen dataset:
  `replay:204eca70-c64a-4539-9bde-2f504f5f68af`
- Name: `vps-daily-training-30-20260728-v1`
- Unique training labels: `30`
- Replay items: `3359`
- Training cutoff:
  `2026-07-27T16:13:16.984047+00:00`
- Cutoff label:
  `label:88dce179-e282-434a-9323-cdcbb08f8b30`
- Manifest SHA-256:
  `da7b245a89cd1e03f2d25137f1aa86da791ecc4f0cf0fff4df0928cfa03a370e`
- Offline verification: `3359/3359`
- Post-build database SHA-256:
  `ff1d594d2608daf42655bd4bd258f5dd0c6838479ee50930519ce5111de57b0c`

The four labels available immediately after the training cutoff were not valid
OOS: every selected decision occurred before all frozen training labels were
available. No combined replay or calibration was created from that snapshot.

## OOS Artifact

Five later labels passed the strict condition:

```text
every frozen training label_available_at < OOS decision_at
```

A second WAL-consistent snapshot was created at:

```text
/opt/polymarket-crypto-threshold/backups/phase2-oos-20260729/
crypto-threshold-20260728T173136.176463Z.db
```

Before replay construction:

- SHA-256:
  `3d81adf371605c364d486dd33995a51ae58f1029cb72ab8f4cd776baa881b5e0`
- Schema: `v5`
- `integrity_check=ok`
- Foreign-key violations: `0`
- Replay datasets/items: `0/0`
- Calibration runs: `0`

The frozen training replay was independently reproduced in this snapshot:

- Reproduced dataset:
  `replay:acadfd79-379b-48b2-a71f-0721457d0ff8`
- Items and labels: `3359 / 30`
- Offline verification: `3359/3359`
- Cutoff and selected labels: exact match
- Manifest SHA-256: exact match
  `da7b245a89cd1e03f2d25137f1aa86da791ecc4f0cf0fff4df0928cfa03a370e`

The combined replay then bound itself to that frozen dataset:

- Combined dataset:
  `replay:d9adcb2c-fcec-49b2-b24b-7c793e4a5d39`
- Name: `vps-daily-combined-20260729-v1`
- Unique labels: `39`
- Replay items: `3984`
- Manifest SHA-256:
  `bb818edcf9b288687f08bcb22a93a852bd9ebe94ab3dfe3ea7512d032ed1c9ab`
- Offline verification: `3984/3984`

## OOS Composition

The five valid OOS labels are all one BTC ladder with the same
`2026-07-28T16:00:00Z` settlement:

| Strike | Outcome | Binance close |
|---:|:---:|---:|
| $56,000 | Yes | 63,926.66 |
| $58,000 | Yes | 63,926.66 |
| $60,000 | Yes | 63,926.66 |
| $62,000 | Yes | 63,926.66 |
| $64,000 | No | 63,926.66 |

These are five unique contract labels and valid chronological OOS rows, but
they are highly correlated and do not represent five independent market events.

## Calibration

- Run:
  `calibration:dbc1dc3e-638d-4b2a-9688-b4531d4baf7a`
- Method: `frozen_training_unique_label_histogram_laplace`
- Source: `fixed-holdout-calibration-v3`
- Model: `histogram-laplace-frozen-training-v3`
- Bins: `10`
- Minimum frozen training size: `30`
- Replay samples: `39`
- Evaluated OOS labels: `5`
- Status: `complete`

Lower is better for all metrics:

| Probability source | Brier | Log loss | ECE |
|---|---:|---:|---:|
| Polymarket midpoint | **0.00420955** | **0.03303304** | **0.03070000** |
| Raw model | 0.01123915 | 0.05491243 | 0.04820960 |
| Calibrated model | 0.05138889 | 0.17267713 | 0.13333333 |

The market midpoint wins every metric. The calibrated result is worse than the
raw model. This run therefore provides no evidence that the model beats the
market, no profitability evidence, and no basis for live trading.

## Mechanical Acceptance

The authoritative command returned exit code `0`:

```text
crypto-threshold phase2-acceptance \
  --db /opt/polymarket-crypto-threshold/backups/phase2-oos-20260729/crypto-threshold-20260728T173136.176463Z.db \
  --output /opt/polymarket-crypto-threshold/backups/phase2-oos-20260729/PHASE2-ACCEPTANCE-REPORT-20260729.md
```

All nine checks passed:

1. `schema_integrity`
2. `no_trading_mutation_surface`
3. `replay_dataset`
4. `chronological_train_and_oos`
5. `calibration_metrics`
6. `shadow_72h_coverage`
7. `cycle_rest_rejection_paper_evidence`
8. `external_schema_drift_monitoring`
9. `binance_websocket_evidence`

Key evidence:

- Continuous qualifying segment: `73.0584` hours / `1355` cycles
- Total cycles in accepted snapshot: `1484`
- Monitored external payloads without drift: `146423`
- Replay datasets: `2`
- Replay items: `7343`
- Calibration runs: `1`
- Forbidden trading tables: none
- Final DB integrity: `ok`
- Final DB foreign-key violations: `0`

Mechanical report SHA-256:

```text
c347dccdcb6a493046149d4b6f6bfd96e95108b25bd206e4d6fee5aaa1b8ca84
```

Final accepted evidence DB SHA-256:

```text
a2401aeae0a84e602ff52133864082477762ba3a021e13acd177fe9291300e8e
```

## Runtime Isolation

The accepted replay/calibration writes occurred only in independent snapshots.
The actively collected source database remained:

```text
replay_datasets=0
replay_items=0
calibration_runs=0
```

At final observation:

- Forward service: `active/running`, PID `152184`, zero restarts
- Up/Down service: `active/running`, PID `152162`, zero restarts
- Latest cycles: `complete_rest_fallback`
- No service restart was performed during acceptance

## Local Quality Gates

```text
uv run pytest -q
242 passed in 9.99s

uv run ruff check src/ tests/
All checks passed!

UV_CACHE_DIR=/tmp/crypto-threshold-uv-cache uv run mypy src/
Success: no issues found in 53 source files

git diff --check
clean
```

The first mypy invocation did not reach type checking because the sandbox
blocked the default uv cache under `~/.cache/uv`. The authoritative rerun used
an isolated `/tmp` cache and passed.

## Final Review

Phase 2 is accepted as an auditable read-only research and evidence system. It
has demonstrated exact-input replay, strict chronology, fixed-holdout
evaluation, long-running REST fallback, stream evidence, paper-ledger
operation, schema monitoring, and absence of a trading mutation surface.

Phase 2 has not demonstrated model edge. The next research gate is a larger,
event-diverse OOS set across multiple dates and assets, followed by a
predeclared calibration challenger comparison. Phase 3 live trading is not
approved.

## Safety Attestation

- Real BUY/SELL executed: **No**
- Order signing or submission: **No**
- Authenticated reconciliation: **No**
- Private key requested or used: **No**
- Active source DB replay/calibration mutation: **No**
- Service restart during acceptance: **No**
- Live status after acceptance: **NO-GO**
