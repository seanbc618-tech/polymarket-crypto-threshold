# Project Status

**Date:** 2026-07-25

**Classification:** Research prototype

**Live status:** **NO-GO**

## Current Truth

Phase 0 truth repair, the Phase 1 read-only research loop, and the Phase 2
software path are implemented. The system can discover, analyze, label, replay,
calibrate, and paper-monitor real public Polymarket markets, but it is not a
trading bot and is not approved for live capital.

Phase 2 empirical acceptance is still pending. The local five-hour smoke
completed cleanly and produced a verified replay, but its 132 decision snapshots
represent only five unique settlement labels and no later out-of-sample window.
The current local development network requires an explicit outbound proxy for
Binance WebSocket access; the proxied stream produced valid BTCUSDT and ETHUSDT
1m Close ticks. A separate direct-connect Hong Kong VPS deployment is now
running the formal Phase 2 shadow window against a fresh evidence database.
Reconnect evidence and unique-label out-of-sample calibration remain pending.
These are evidence gaps, not successes.

A separate read-only research expansion now supports the currently observed
5-minute and 15-minute Polymarket Up/Down contract family for BTC, ETH, SOL,
XRP, DOGE, BNB, and HYPE. These are Chainlink USD Data Stream contracts, not
Binance daily thresholds. They use the same canonical discovery, workflow, and
repository ownership path, but run with a separate configuration, evidence
database, replay family, and VPS systemd unit. This expansion is shadow
research only and does not make Phase 2 accepted. The independent
`crypto-threshold-updown-shadow.service` has now been deployed on the
direct-connect VPS against
`/opt/polymarket-crypto-threshold/data/updown-shadow.db`.

No order signer, authenticated trading client, BUY/SELL method, cancellation
path, or position mutation exists in the runtime. Setting
`TRADING_DISABLED=false` makes `doctor` fail.

## Deployment Network Contract

- On the current local macOS/Mainland China development network, an outbound
  proxy is required for networked builds, live-provider tests, and shadow
  monitoring. HTTP clients use the local `HTTP_PROXY`/`HTTPS_PROXY`; the
  official Binance WebSocket SDK must additionally receive
  `BINANCE_STREAM_PROXY_URL=http://127.0.0.1:12334`.
- The proxy is transport configuration only. It does not weaken
  `TRADING_DISABLED`, enable authenticated channels, or authorize live trading.
- The Hong Kong VPS deployment follows the weather-system model and connects
  directly: `BINANCE_STREAM_PROXY_URL` is blank and local
  `HTTP_PROXY`/`HTTPS_PROXY` values are unset. Do not copy a loopback proxy
  address into VPS configuration.
- Local network evidence is not VPS readiness evidence. The VPS has rerun
  `doctor`, public REST/WebSocket checks, shadow monitoring, fallback checks,
  and the mechanical acceptance command against VPS-generated evidence.
- The daily process has no private key, funder, authenticated channel, or proxy
  environment. It remains `TRADING_DISABLED=true`, uses
  `POLYMARKET_STREAM_USER_CHANNEL_ENABLED=false`, and writes only to
  `/opt/polymarket-crypto-threshold/data/phase2-vps.db`.
- The independent short-Up/Down process has the same no-secret/no-proxy safety
  boundary, disables Binance streaming, enables only the public Chainlink
  reference stream, and writes only to
  `/opt/polymarket-crypto-threshold/data/updown-shadow.db`.

## Implemented Scope

- One canonical `markets` table and one `Repository` SQL boundary.
- `DiscoveryService -> MarketWorkflowService -> Repository` ownership.
- Raw Gamma, CLOB book, fee, Binance kline, and Coinbase payload persistence.
- Authoritative settlement contract with event/condition/token IDs, source,
  pair, strict operator, strike, candle, field, timezone, observation time,
  target UTC, Gamma `endDate`, parser version, and raw description.
- Public YES/NO order books with explicit outcome-to-token mapping.
- Target-size executable ask VWAP; midpoint is retained only as a diagnostic.
- Per-market CLOB fee metadata and net EV after fee, spread, and slippage.
- Binance settlement-aligned one-minute close and historical realized
  volatility; Coinbase is a USD sanity check only.
- Structured rejection signals for every blocked analysis.
- A canonical seven-asset registry separates daily-threshold capability from
  short-Up/Down capability instead of assuming every asset has every contract.
- Authoritative parsing for 5m/15m `Up`/`Down` markets requires Chainlink,
  the exact USD pair, exact window start/end, `data_stream_value`, UTC,
  `Up >= start`, both token IDs, open order-book status, and all identifiers.
- The official public SDK `CryptoPricesSpec(topic="prices.crypto.chainlink")`
  supplies bounded/coalesced in-memory ticks for BTC/USD, ETH/USD, SOL/USD,
  XRP/USD, DOGE/USD, BNB/USD, and HYPE/USD. The stream has an idempotent
  lifecycle, reconnect generation, freshness checks, bounded per-pair history,
  and scrubbed health output; it never writes SQLite.
- Short-window model inputs persist the exact start tick, current tick, and
  trailing volatility window before the signal. Missing the start boundary is
  a hard rejection; Binance is never substituted for a Chainlink contract.
- Short-window net EV still uses token-specific REST books, full ask-depth
  VWAP, and the per-market fee schedule. Stream BBO is only a reprice hint.
- Chainlink settlement labels require Gamma's public `priceToBeat`,
  `finalPrice`, and final Up/Down outcome to agree. Equality resolves to Up.
  Incomplete resolution metadata remains pending and is retried.
- Replay manifests are explicitly family-scoped. Daily Binance and short
  Chainlink labels cannot be mixed, and a signal boundary must match the
  authoritative settlement boundary before entering replay.
- Versioned SQLite initialization/migration, foreign keys, WAL, transactions,
  source/model versions, and observed/received timestamps.
- Fail-closed `doctor` checks for DB, providers, HTTPS URLs, and trading mode.
- Optional official-SDK `PolymarketStreamBridge` with coalesced bounded queues,
  idempotent lifecycle, token freshness, reconnect/resubscribe, REST backfill,
  periodic verification, and recursively scrubbed health output.
- Crypto stream subscriptions grouped by stable `event_id`, with a settlement
  contract tuple fallback. Active and candidate ladders include every sibling
  YES/NO token; protected position/open-order inputs cannot be displaced.
- Stream BBO changes only enqueue complete ladders for the canonical
  `MarketWorkflowService`; executable depth and every resulting signal remain
  token-specific REST calculations.
- Schema v3 exact evidence links: every signal has an `analysis_run_id` and
  explicit `analysis_signal_inputs` rows for only the raw payloads consumed by
  that decision. The old maximum-payload boundary remains diagnostic only.
- Contract-authoritative settlement labels use the exact Binance one-minute
  candle opened at `target_time_utc`, require the candle to be closed, and
  apply strict `>`/`<` semantics. Raw Binance payloads are persisted first.
- Immutable replay manifests hash model features and exact input payloads.
  SQLite triggers prevent mutation of settlement labels, sealed datasets, and
  replay items. Empty datasets explicitly fail replay acceptance.
- Chronological walk-forward histogram calibration v2 uses only labels
  available before each test decision and deterministically selects the latest
  pre-deadline snapshot for each unique settlement label. Repeated snapshots
  remain in replay for audit but cannot impersonate independent training labels.
  Reports include Brier score, log loss, and ECE for raw, calibrated, and
  Polymarket midpoint-baseline probabilities.
- A persistent paper ledger records every hypothetical enter/skip decision,
  deduplicates by signal/policy, prevents a second open entry for one market,
  and settles only from persisted Binance labels.
- `shadow --once` and continuous `shadow` keep orchestration and SQLite writes
  on the main thread. Stream changes select candidates; initial subscription,
  stale data, reconnect, and verification expiry force REST fallback.
- `phase2-acceptance --db <evidence.db> --output <report.md>` opens the supplied
  SQLite database in read-only/query-only mode and fails closed on schema,
  replay, chronological OOS, metrics, continuous shadow, fallback/paper, and
  Binance stream evidence. Stored calibration counts are checked against unique
  replay labels and actual OOS chronology; sparse endpoints cannot impersonate
  a 72-hour continuous run.
- Every shadow cycle checks newly persisted Gamma, CLOB, Binance, and Coinbase
  raw payloads against versioned transport contracts. Counts, source versions,
  and drift codes are stored in the existing cycle health JSON without a second
  evidence table; detected drift or monitor failure degrades the cycle.
- `shadow --duration-hours <hours>` uses a monotonic deadline and always closes
  public REST/WebSocket clients. It rejects private keys from ordinary process
  configuration and cannot be combined with `--once`.
- A separate official-SDK `BinanceReferencePriceStream` normalizes only closed
  BTCUSDT/ETHUSDT 1m Close events with provider/receive timestamps, freshness,
  sequence, payload hash, and source version. It is bounded, coalescing,
  reconnecting, cancellable, public-only, and disabled by default.
- A server-rendered Crypto Dashboard exposes only the existing research truth:
  BTC/ETH contracts, strike and settlement metadata, YES/NO books, executable
  ask VWAP, net EV, replay/calibration results, shadow cycles, and the paper
  ledger. Page renderers query through the canonical `Repository`.
- Local wallet setup reuses one project-specific macOS Keychain item for
  `POLYMARKET_PRIVATE_KEY`, persists only the non-sensitive funder address,
  pins `TRADING_DISABLED=true`, and never reflects the private key into SQLite,
  config files, HTML, logs, or redirects.
- Local wallet POST requests use a process CSRF token and exact local
  Host/Origin checks. Public-origin mode does not load Keychain secrets and
  disables the wallet page and POST route. The server refuses unsafe trading
  mode, enabled User Channel, private keys found in ordinary config, and
  unprotected non-local binds.
- The wallet and readiness pages do not construct `SecureClient`; balances,
  open orders, fills, positions, authenticated reconciliation, and order
  signing remain explicitly disconnected.

## Supported Contracts

Two contract families are isolated:

1. `daily_threshold`: BTC, ETH, SOL, or XRP terminal thresholds using the
   matching Binance USDT pair, final one-minute `Close` at noon
   `America/New_York`, exact `>`/`<`, and a matching Gamma deadline.
2. `short_updown`: BTC, ETH, SOL, XRP, DOGE, BNB, or HYPE 5m/15m windows using
   the matching Chainlink USD Data Stream. The affirmative token is `Up`; the
   exact boundary is end value `>=` start value.

Missing fields are preview-only. Monthly hit/touch, path-dependent, range,
`High`/`Low`, source/pair/field mismatch, stale data, malformed tokens, expired
markets, and short windows whose exact start tick was not captured are rejected.

## Branch Audit

The following commits were reviewed and were not batch cherry-picked:

| Commit | Decision |
|---|---|
| `519b149` | Used only as a design reference; replaced by the canonical service and repository path. |
| `8a09c5c` | Rejected duplicate discovery owner. |
| `8044acd` | Rejected duplicate `discovered_markets` storage. |
| `f425e4b` | Rejected CLI built on duplicate storage ownership. |

There is no runtime `discovery.py` sibling and no `discovered_markets` table.

## Phase 2 Acceptance

Software implementation is complete for the following path:

```text
exact raw inputs -> Binance settlement label -> immutable replay
-> leakage-safe walk-forward calibration -> stream-triggered REST shadow
-> persistent paper ledger
```

Empirical acceptance remains open and must not be replaced with synthetic test
results:

- Collect at least 30 unique chronologically prior settlement labels and a
  separate later out-of-sample evaluation window.
- Verify the sealed replay dataset at 100%; empty datasets are failures.
- Publish raw, calibrated, and market-baseline Brier/log-loss/ECE without
  claiming improvement when the measurements do not support it.
- The bounded 5-hour local proxy-backed smoke is complete with cycle, fallback,
  rejection, paper-ledger, and external schema-drift evidence.
- Complete the active direct-connect VPS shadow window for at least 72 persisted
  hours. This remains the mechanical acceptance threshold.
- A valid closed Binance 1m stream tick has been demonstrated on the deployment
  network; reconnect evidence remains pending. REST must remain operational
  when the stream cannot do so.
- Reconfirm that no order, fill, position, signer, or authenticated trading
  mutation exists, and that any configured private key remains Keychain-only
  and disconnected.

Until all items are evidenced, Phase 2 is **implemented but not accepted**.
An exit code `0` from the mechanical database checker still requires final
project review and never authorizes live capital or Phase 3 trading work.

## Verification Evidence

- Current local gate on 2026-07-25: `212 passed`; Ruff reported no findings;
  mypy reported no issues in 52 source files; `git diff --check` was clean.
- The short-Up/Down directed suite covers all 7 assets across both intervals,
  official public SDK-only imports, normalization, bounded/coalesced history,
  reconnect, lifecycle, secret-safe health, Gamma tag discovery, Up/Down token
  mapping, parser mismatches, boundary hard rejection, exact-input persistence,
  Chainlink tie settlement, family-scoped replay, and one-cycle 14-market
  orchestration.
- An isolated direct-connect VPS candidate preflight on 2026-07-25 initialized
  schema v4, passed `doctor`, returned public CLOB time, and received fresh
  Chainlink ticks for all seven pairs without a proxy or credentials.
- The bounded isolated candidate completed 17 cycles over 20 minutes:
  15 `complete_rest_fallback` and two safely degraded on
  `reconciliation_hint_pending_rest`. It persisted 238 signals, including 156
  analyzed signals across both 5m and 15m markets, and 82 explicit rejections.
  It created 13 hypothetical paper entries, of which five settled from 21
  public Gamma labels. All label provider/pair/interval/field/operator values
  matched the contracts, all dynamic signal boundaries matched Gamma's
  `priceToBeat`, and all `>=` outcomes recomputed without mismatch. The five
  settled entries' aggregate `+26.867898 USDC` is operational evidence from a
  tiny correlated sample, not evidence of profitability.
- The formal independent Up/Down service started on 2026-07-25 at
  `08:41:02 UTC`. Its fail-closed `doctor` passed schema v4, WAL, foreign keys,
  Gamma/CLOB reads, live NO-GO, and fresh direct Chainlink ticks for all seven
  pairs. The process environment has zero non-empty credentials and zero
  proxies. Initial cycles correctly rejected windows that predated startup and
  then rejected the first common boundary until the configured 15-minute
  volatility history had warmed up. At the next common boundary, all seven 15m
  markets and six 5m markets entered `analyzed`; the XRP 5m market was isolated
  as `stale_or_missing_chainlink_current_tick`. The following cycle recovered
  without a restart and persisted seven analyzed markets for each interval.
  Four hypothetical paper entries were recorded, with no order/fill/position or
  other trading-mutation table.
- Final local deployment gate on 2026-07-24: `151 passed`; Ruff reported no
  findings; mypy reported no issues in 51 source files; `git diff --check` was
  clean.
- The unit/integration suite covers contract semantics, DST, exact
  boundaries, token mapping, books, fees, price identity/freshness, storage
  ordering, migrations, discovery idempotency, CLI safety, stream lifecycle,
  reconnect/backfill, ladder selection, REST fallback, exact signal inputs,
  strict settlement, immutable replay, no-lookahead calibration, paper
  idempotency, stream-triggered shadow selection, and no trade surface.
- Public Gamma GET smoke check on 2026-07-22 confirmed current daily contract
  wording and also confirmed that unsupported monthly hit/High markets coexist
  in search results and must be rejected by the parser.
- A public `shadow --once` smoke on 2026-07-23 used schema v3 in
  `/tmp/crypto-threshold-phase2-shadow.db`: 8 markets discovered, 2 analyzed,
  both safely rejected for incomplete executable asks, 2 paper skips, 16 exact
  signal-input links, and no order/fill/position tables.
- During that smoke the Polymarket stream reported REST fallback and analysis
  completed through REST. Direct official Binance SDK attempts on both 9443
  and 443 timed out in the current network; cancellation completed cleanly
  without an unclosed client session.
- The acceptance checker inspected the legacy local `crypto_threshold.db` as
  `PENDING/NOT ACCEPTED`: it lacks schema v3 research tables and still contains
  historical order/position tables. The database SHA-256 was identical before
  and after inspection, confirming that the smoke did not mutate the evidence
  file.
- A fresh Phase 2 preflight on 2026-07-23 used
  `runs/phase2-acceptance-20260723/evidence.db`: 20 markets, 100 persisted raw
  payloads, 10 analyzed candidates, 10 paper skips, zero paper enters, and one
  complete REST-fallback cycle. All 100 Gamma/CLOB/Binance/Coinbase payloads
  passed the versioned schema monitor. The mechanical report correctly remained
  `PENDING/NOT ACCEPTED`.
- The bounded local smoke started on 2026-07-23 at approximately `09:05 UTC`
  (`17:05 Asia/Shanghai`) and exited cleanly at `22:06:04 Asia/Shanghai`. Its
  dedicated database is
  `runs/phase2-acceptance-20260723/continuous.db` and its unbuffered log is
  `runs/phase2-acceptance-20260723/continuous.log`. After the explicit proxy and
  Binance SDK compatibility fix in `caa3830`, the stream persisted fresh closed
  BTCUSDT and ETHUSDT 1m Close ticks while REST remained authoritative.
  The database contains 86 complete cycles spanning 4.9525 recorded hours,
  with maximum gap 180.051 seconds and maximum cycle duration 95.129 seconds.
  All 86 schema-drift checks passed over 8,588 payloads; 150 Binance stream
  ticks were drained. No reconnect occurred, so that gate remains pending.
- On 2026-07-24, all 15 due tradable contracts received immutable Binance
  settlement labels. The sealed `local-smoke-20260723-v1` replay contains 132
  exact-input snapshots and verified 132/132, but those snapshots cover only
  five unique labels. Calibration v2 therefore reports `samples=5`,
  `evaluated=0`, and `no_valid_walk_forward_test_window`.
- The paper ledger settled its two open hypothetical entries for a combined
  `+2.191591 USDC`; 858 decisions were skips. Two correlated paper outcomes are
  operational evidence only and are not evidence of profitability.
- The local smoke database's mechanical report passes schema integrity,
  no-trading surface, replay verification, cycle/REST/rejection/paper evidence,
  and external schema drift. It remains `PENDING/NOT ACCEPTED` on unique-label
  OOS calibration, formal VPS 72-hour coverage, and Binance reconnect evidence.
- The public VPS application tree now contains the short-Up/Down feature commit
  `01b78da` under the independent `crypto-threshold` system user. The original
  daily service retained PID `49268`, zero restarts, and its original
  `2026-07-24 13:39:56 CST` start time throughout the in-place source update.
  The weather autopilot was not modified. No Crypto Dashboard or wallet setup
  was enabled.
- Direct VPS `doctor` passed Gamma, CLOB, Binance REST, Coinbase REST, schema v3
  WAL/foreign-key checks, shadow mode, and live NO-GO. An official Binance SDK
  probe received fresh closed BTCUSDT and ETHUSDT `1m Close` ticks with
  timestamps, sequence, and payload hashes while `proxy_enabled=false`.
- Initial VPS probes exposed an unsynchronized host clock. The pre-NTP evidence
  was stopped and preserved as
  `/opt/polymarket-crypto-threshold/backups/pre-ntp-phase2-vps-20260724T053327Z.db`.
  `systemd-timesyncd` now uses explicit IPv4 NTP sources and reported
  `NTPSynchronized=yes`; the observed `+2.824872s` correction removed the
  received-before-provider timestamp violation. Shadow startup now waits for
  `systemd-time-wait-sync.service`.
- The fresh formal evidence window began at
  `2026-07-24T05:34:07.579835+00:00`. After a bounded deployment-unit restart,
  three cycles remained continuous with no gap above 300 seconds. The active
  process uses a 73-hour wall-clock bound so the 180-second cadence can still
  produce at least 72 persisted hours.
- The synchronized VPS baseline has real BTCUSDT/ETHUSDT closed ticks with
  `received_at >= provider_timestamp`, Polymarket REST fallback, paper skips,
  and clean schema-drift evidence. Its mechanical report is correctly
  `PENDING/NOT ACCEPTED`: closed-tick evidence now passes, while replay,
  calibration, 72-hour coverage, and reconnect remain pending.
- `crypto-threshold-backup.timer` is active. A manual WAL-consistent backup
  completed with `PRAGMA integrity_check=ok`; source and backup contained no
  order/fill/position/signer/reconciliation tables.
- `crypto-threshold-updown-backup.timer` is independently scheduled for
  `04:00 CST`. Its deployment smoke exposed transient `.partial-wal` and
  `.partial-shm` sidecars; the backup helper now normalizes the destination to
  DELETE journal mode and removes all temporary sidecars before retention.
- Exact final test, lint, type-check, diff-check, and commit evidence is recorded
  in the delivery report for the Phase 2 commit.

## Known Risks

- Gamma search is relevance-based and is not a completeness guarantee.
- Public REST snapshots are not atomic across providers.
- The Chainlink RTDS start tick must be compared with Gamma's eventual
  `priceToBeat` over real closed windows. Any mismatch excludes that signal
  from replay; enough forward evidence has not yet accumulated to characterize
  this risk.
- A process started or reconnected after a 5m/15m boundary cannot reconstruct
  the beginning value from the live stream and rejects that active window.
- A fresh short-Up/Down process also needs the configured trailing volatility
  history before it can analyze a boundary; warm-up rejections are persisted
  and cannot be treated as market/model failures.
- Gamma's 5m/15m discovery metadata can report misleading recurrence values;
  discovery therefore verifies series slug and exact window duration.
- Stream Market Channel data is BBO-only acceleration, not L2 executable depth;
  REST remains mandatory for VWAP and final validation.
- The official `polymarket-client` release is a beta API and can change.
- Public Binance WebSocket connectivity on the local development network
  requires the explicit local proxy. Direct VPS connectivity is verified, but
  a natural or controlled reconnect has not yet been persisted. The stream
  remains opt-in and REST remains authoritative.
- Coinbase USD versus Binance USDT is only a sanity comparison, not the
  settlement source.
- The terminal GBM model has only five unique settled contract labels and no
  valid out-of-sample calibration window.
- API schemas and fee behavior can change; parser and adapter versions make
  resulting records auditable but do not remove schema-drift risk.

## Next Gate

Collect forward decisions for new daily contracts, settle them after their
authoritative Binance candle closes, and rebuild replay until at least 30 unique
prior labels exist. Then collect and settle a later decision batch for the
independent OOS window and publish calibration metrics. Repeating the five-hour
local smoke is unnecessary. Keep the active VPS monitor uninterrupted, verify
its daily backup and cycle-gap health, capture reconnect evidence, and run the
mechanical acceptance checker after at least 72 persisted hours. Continue
settling new daily contracts and rebuild replay/calibration when enough unique
labels exist. Live order placement remains explicitly outside this phase and
requires a separate design and approval.

In parallel, keep the short-Up/Down evidence in its separate database. Compare
captured starts with Gamma's eventual `priceToBeat`, settle all seven assets,
then build and verify a `short_updown` replay before interpreting any paper
result. Do not merge this evidence into the daily Phase 2 acceptance run.

After the formal daily VPS monitor is stopped, backed up, and reviewed, remind
the operator that the old local smoke history can be deleted. Do not delete it
while the formal acceptance window is still active.
