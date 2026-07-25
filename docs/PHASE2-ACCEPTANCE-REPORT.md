# Phase 2 Acceptance Report

**Tool:** `crypto-threshold phase2-acceptance --db <evidence.db> --output <report.md>`

**Empirical status:** **PENDING / NOT ACCEPTED** until a real evidence database
passes every mechanical gate. Software completeness is not acceptance.

## Purpose

Phase 2 acceptance is a **fail-closed, read-only** inspection of an existing
SQLite evidence database. It never:

- opens the evidence database in write mode
- creates migrations or new production tables
- mutates probability, calibration, or net-EV logic
- fabricates production evidence
- touches private keys, Keychain, wallets, or trading surfaces
- announces acceptance without concrete rows that satisfy every check

Any missing gate yields `PENDING/NOT ACCEPTED`. The tool does not speculate.

## Command

```bash
crypto-threshold phase2-acceptance \
  --db /path/to/evidence.db \
  --output /path/to/report.md
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Every mechanical DB gate passed; final project approval remains separate |
| `1` | At least one gate failed → `PENDING/NOT ACCEPTED` |
| `2` | Evidence DB missing or inspection error |

## Mechanical gates

| Check | Pass criterion (all evidence must already exist) |
|---|---|
| `schema_integrity` | `schema_meta.version == 4`, foreign keys ON, no FK violations, WAL journal, required research tables present |
| `no_trading_mutation_surface` | No orders/fills/positions/signer/authenticated-reconciliation mutation tables |
| `replay_dataset` | At least one **sealed**, **non-empty** replay dataset that **verifies at 100%** |
| `chronological_train_and_oos` | One complete run references a verified sealed dataset; actual replay rows provide ≥30 labels available before an OOS decision, and stored sample/evaluation counts equal recomputed counts |
| `calibration_metrics` | That same qualifying run has finite raw / calibrated / market-midpoint **Brier**, **log loss**, and **ECE** |
| `shadow_72h_coverage` | `shadow_cycles` span ≥72 hours with no inter-cycle gap above 300 seconds and no cycle duration above 900 seconds |
| `cycle_rest_rejection_paper_evidence` | Shadow cycle rows + REST fallback evidence + rejected signals with reasons + `paper_ledger` rows |
| `external_schema_drift_monitoring` | Every cycle contains the versioned public-payload monitor result, at least one payload was checked, and no drift/monitor failure was observed |
| `binance_websocket_evidence` | Shadow health contains a fresh normalized Binance BTCUSDT/ETHUSDT closed 1m Close tick with timestamps/hash plus a connected reconnect `generation >= 2` |

## Evidence sources (existing tables only)

- `replay_datasets` / `replay_items` + offline `ReplayService.verify`
- `calibration_runs.metrics_json`
- `shadow_cycles` (`started_at`, `completed_at`, `status`, `stream_health_json`, `reasons`)
- `analysis_signals` with `status = 'rejected'` and non-empty `reasons`
- `paper_ledger`
- `schema_meta` and `sqlite_master`

No authenticated trading tables are part of the research schema. Their absence is
required; inventing them is a hard fail.

The evidence database is opened through SQLite URI `mode=ro` with
`query_only=ON`. The report path is rejected if it resolves to the database path.

## What this report is not

- Not a claim that the current development database has passed Phase 2
- Not a substitute for a 72-hour real shadow run on the deployment network
- Not permission to enable live capital, order placement, or signing
- Not an automatic Phase 3 gate or substitute for final code/evidence review
- Not a calibration quality claim: metrics must be present and finite, but the
  checker does **not** assert model improvement over market baseline

## Implementation boundary

Allowed surface for this gate:

1. One acceptance service: `Phase2AcceptanceService`
2. A few read-only `Repository` queries
3. One CLI entry: `phase2-acceptance`
4. Unit/CLI tests under `tests/test_phase2_acceptance.py`
5. This document

Out of scope for acceptance work:

- New tables/migrations
- Probability model, calibration algorithm, or net EV changes
- Dashboard, wallet, Keychain, security boundary changes
- WebSocket state machine changes
- Synthetic production evidence in real acceptance databases
- Private keys or exchange mutation paths

## Current project truth

As of the Phase 2 software path, empirical acceptance remains open. Collect real
settled observations, seal and verify a non-empty replay dataset, run walk-forward
calibration with ≥30 chronological training labels plus an OOS window, complete a
72-hour continuous shadow window with REST fallback / rejection / paper evidence,
and demonstrate closed Binance 1m stream ticks with reconnect on the deployment
network. Until every gate is evidenced in the inspected database, Phase 2 is
**implemented but not accepted**.
