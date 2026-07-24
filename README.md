# Polymarket Crypto Threshold Research

Auditable, read-only analysis for a narrow class of Polymarket BTC/ETH price
threshold markets.

## Status

**Research prototype. Live trading is NO-GO.**

The runtime contains public market-data reads, SQLite audit persistence, replay,
calibration, and a paper ledger. It does not contain order signing, BUY/SELL
placement, cancellation, or position mutation. `TRADING_DISABLED` must remain
`true`.

Phase 1 supports only contracts whose binding text provides all of these facts:

- BTC or ETH
- Binance `BTC/USDT` or `ETH/USDT`
- one-minute candle `Close`
- a terminal daily threshold with an exact `>` or `<` boundary
- noon in `America/New_York`, converted with DST rules
- Gamma event, condition, YES/NO token IDs, and matching `endDate`

Anything missing, expired, path-dependent, or semantically mismatched is saved
for preview and rejected from analysis.

## Workflow

```text
Gamma discovery
  -> raw payload + canonical market
  -> authoritative contract parser
  -> YES/NO CLOB books + per-market fee schedule
  -> Binance settlement-aligned close + historical klines
  -> Coinbase sanity check
  -> target-size ask VWAP + fee/spread/slippage net EV
  -> persisted signal or persisted rejection reasons
```

An optional official-SDK Polymarket Market Channel bridge can supply token BBO
change hints. It is disabled by default and shadow/read-only when enabled. Its
BBO never replaces REST depth: every net-EV analysis still fetches fresh YES
and NO CLOB order books before calculating ask VWAP, fees, or slippage.

Phase 2 adds immutable exact-input replay manifests, Binance settlement labels,
leakage-safe walk-forward calibration, a persistent paper ledger, and optional
Polymarket/Binance stream hints. Streams select work only; every model input and
executable VWAP is refreshed and persisted through REST.

The production ownership path is:

```text
DiscoveryService -> MarketWorkflowService -> Repository
```

## Quick Start

```bash
uv sync
uv run crypto-threshold init-db
uv run crypto-threshold doctor
uv run crypto-threshold discover --asset BTC
uv run crypto-threshold markets
uv run crypto-threshold analyze --market <gamma_market_id>
uv run crypto-threshold settle
uv run crypto-threshold replay-build --name <dataset_name>
uv run crypto-threshold replay-verify --dataset <dataset_name>
uv run crypto-threshold calibrate --dataset <dataset_name>
```

`analyze` accepts a real Gamma market ID or condition ID. It does not accept an
operator-supplied market probability. A rejected analysis exits with code 2 but
still records its inputs and reasons.

Continuous monitoring is opt-in:

```bash
SHADOW_ENABLED=true uv run crypto-threshold shadow --once
SHADOW_ENABLED=true uv run crypto-threshold shadow --duration-hours 5
```

The bounded five-hour command is the local proxy-backed smoke target. It does
not lower the 72-hour mechanical acceptance threshold; that formal run is
deferred to a fresh direct-connect VPS evidence database.

`POLYMARKET_STREAM_ENABLED` and `BINANCE_REFERENCE_STREAM_ENABLED` both default
to `false`. Stream failure falls back to REST. Phase 2 is software-complete but
not empirically accepted until `PROJECT-STATUS.md`'s real-data gates are met.
The bounded run stops cleanly at its wall-clock deadline. Every cycle records a
public-payload schema-drift summary for Gamma, CLOB, Binance, and Coinbase in
the existing `shadow_cycles.stream_health_json`; no second evidence table is
created. Shadow refuses to start when a private key is present in the process
environment or ordinary configuration.

The local Mainland China development network requires an outbound proxy for
networked provider checks and shadow monitoring. Configure
`BINANCE_STREAM_PROXY_URL=http://127.0.0.1:12334` in addition to the local HTTP
proxy environment. The VPS templates under `deploy/` omit all proxy settings,
require synchronized host time, and retain REST as the final analysis
authority.

Mechanical evidence can be checked without opening the database for writes:

```bash
uv run crypto-threshold phase2-acceptance \
  --db <evidence.db> \
  --output <report.md>
```

## Read-only Dashboard

Initialize the database, keep `TRADING_DISABLED=true`, and start the local
server:

```bash
uv run crypto-threshold init-db
uv run crypto-threshold dashboard
```

Open `http://127.0.0.1:8765`. The server renders:

- BTC/ETH contracts with strike, settlement time, YES/NO books, ask VWAP, and net EV
- exact market evidence and rejection reasons
- replay/calibration results
- shadow-cycle evidence
- the persistent paper ledger and settled paper PnL
- Phase 2 readiness and wallet configuration status

The wallet page stores `POLYMARKET_PRIVATE_KEY` only in the project-specific
macOS Keychain item and stores only the non-sensitive `POLYMARKET_FUNDER` in
`.env`. Existing non-empty private keys in `.env` cause the dashboard to refuse
startup. Every POST is protected by a process CSRF token plus Host/Origin
validation.

Wallet setup does not construct `SecureClient`, enable the Polymarket User
Channel, read account balances, sign an order, or submit/cancel anything.
Authenticated account reconciliation remains outside Phase 2. A non-local bind
is refused unless `DASHBOARD_PUBLIC_ORIGIN` is an exact HTTPS origin and the
operator has supplied a trusted external access boundary. Public-origin mode
does not load Keychain secrets and returns `403` for the wallet page and POST
route; wallet setup remains loopback-only.

## Verification

```bash
uv run pytest -q
uv run ruff check src/ tests/
uv run mypy src/crypto_threshold
git diff --check
```

See [PROJECT-STATUS.md](docs/PROJECT-STATUS.md) for current evidence, safety
boundaries, and remaining risks.

## License

Released under the [MIT License](LICENSE).
