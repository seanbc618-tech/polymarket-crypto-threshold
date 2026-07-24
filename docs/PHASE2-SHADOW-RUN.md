# Phase 2 Shadow Evidence Run

## Safety Boundary

This run uses public Gamma, Polymarket CLOB, Binance, and Coinbase data only.
It must keep:

- `TRADING_DISABLED=true`
- `POLYMARKET_PRIVATE_KEY=` in the process environment
- `POLYMARKET_STREAM_SHADOW_MODE=true`
- `POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false`

No secure client, signer, BUY/SELL, order cancellation, authenticated
reconciliation, or position mutation is part of this run.

## Preflight

On the current local development network, Hiddify must be listening on
`127.0.0.1:12334`. Use a new evidence database under ignored `runs/`, then run
exactly one cycle:

```bash
env \
  DATABASE_PATH="$PWD/runs/phase2-acceptance-20260723/evidence.db" \
  SHADOW_ENABLED=true \
  TRADING_DISABLED=true \
  POLYMARKET_PRIVATE_KEY= \
  POLYMARKET_STREAM_ENABLED=true \
  POLYMARKET_STREAM_SHADOW_MODE=true \
  POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false \
  BINANCE_REFERENCE_STREAM_ENABLED=true \
  BINANCE_STREAM_PROXY_URL=http://127.0.0.1:12334 \
  SHADOW_INTERVAL_SECONDS=180 \
  uv run crypto-threshold shadow --once
```

Do not start the continuous run unless the cycle persists:

- schema v3 with no forbidden trading tables
- raw external payloads
- analysis signals and structured rejection reasons
- paper-ledger enter/skip decisions
- one `shadow_cycles` row
- `schema_drift.status = ok`
- REST completion when either WebSocket is unavailable

## Local 5-Hour Smoke

```bash
env \
  DATABASE_PATH="$PWD/runs/phase2-acceptance-20260723/continuous.db" \
  SHADOW_ENABLED=true \
  TRADING_DISABLED=true \
  POLYMARKET_PRIVATE_KEY= \
  POLYMARKET_STREAM_ENABLED=true \
  POLYMARKET_STREAM_SHADOW_MODE=true \
  POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false \
  BINANCE_REFERENCE_STREAM_ENABLED=true \
  BINANCE_STREAM_PROXY_URL=http://127.0.0.1:12334 \
  SHADOW_INTERVAL_SECONDS=180 \
  PYTHONUNBUFFERED=1 \
  uv run crypto-threshold shadow --duration-hours 5
```

`--duration-hours` uses a monotonic wall-clock deadline and stops the monitor and
public HTTP/WebSocket clients in `finally`. A process crash is not acceptance:
restart promptly against the same database and let the mechanical gap check
decide whether continuity was preserved.

This five-hour local run is a proxy-backed stability smoke, not Phase 2
acceptance. The mechanical checker intentionally continues to require 72 hours.

`BINANCE_STREAM_PROXY_URL` is optional and defaults to disabled. When the local
development route is used, the proxy is required and must be configured as an
exact unauthenticated HTTP(S) origin. The official SDK WebSocket path does not
inherit generic `HTTP_PROXY` or `HTTPS_PROXY`; proxy credentials are rejected
and the endpoint is not exposed in stream health.

For a future VPS deployment, do not copy the local proxy setting. Leave
`BINANCE_STREAM_PROXY_URL` blank, unset `HTTP_PROXY` and `HTTPS_PROXY`, and
verify direct REST/WebSocket connectivity on that VPS before starting a fresh
evidence run. Local proxy-backed readiness evidence must not be treated as VPS
network readiness.

## Formal VPS 72-Hour Run

On the future direct-connect VPS, use a fresh evidence database, omit
`BINANCE_STREAM_PROXY_URL`, unset `HTTP_PROXY` and `HTTPS_PROXY`, and run:

```bash
SHADOW_ENABLED=true \
TRADING_DISABLED=true \
POLYMARKET_PRIVATE_KEY= \
POLYMARKET_STREAM_ENABLED=true \
POLYMARKET_STREAM_SHADOW_MODE=true \
POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false \
BINANCE_REFERENCE_STREAM_ENABLED=true \
SHADOW_INTERVAL_SECONDS=180 \
uv run crypto-threshold shadow --duration-hours 72
```

Only this fresh deployment-network run can satisfy the 72-hour mechanical
coverage gate. It still cannot authorize live trading.

## Inspection

The acceptance command opens SQLite with `mode=ro` and `query_only=ON`:

```bash
uv run crypto-threshold phase2-acceptance \
  --db runs/phase2-acceptance-20260723/continuous.db \
  --output runs/phase2-acceptance-20260723/current-acceptance.md
```

Exit code `1` remains expected until all empirical gates are present. Exit code
`0` still requires final review and never authorizes live trading.
