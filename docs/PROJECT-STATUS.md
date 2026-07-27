# Project Status

**Date:** 2026-07-27

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
1m Close ticks. The separate direct-connect Hong Kong VPS deployment completed
its formal bounded Phase 2 shadow window against a fresh evidence database on
2026-07-27. Continuous coverage and reconnect evidence passed. Its immutable
source contains 10 unique settlement labels. A separate v5 working copy is now
collecting forward evidence and reached 20 labels on its first cycle, but it
still has no sealed VPS replay or out-of-sample calibration. These are evidence
gaps, not successes.

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

Commit `0bae0b7` fixes the Up/Down boundary contract and was activated on the
VPS at `2026-07-27T19:45:40+08:00`.
Polymarket's public crypto-window response is the signal and settlement source
for the immutable window `openPrice`; RTDS supplies only the current tick and
trailing volatility. Settlement independently requires the endpoint's
completed `openPrice`/`closePrice`, Gamma's `priceToBeat`/`finalPrice`, and the
resolved outcome to agree. The deployment marker is `0bae0b7` and the new
Up/Down process is PID `136544`; Daily remained inactive after its bounded
completion and forward retained PID `132082`. This correction does not rewrite
or legitimize the 83 historical mismatched signal rows.

On 2026-07-26, the Up/Down service was briefly restarted twice with explicit
owner approval. Commit `b8e69d2` first added schema v5 settlement state; commit
`8866fb2` then corrected the scheduler so due retries and never-attempted
candidates are interleaved within each batch. The daily service was not
restarted and retained PID `49268`. The second deployment showed old pending
markets advancing from attempt 1 to attempt 2 while new candidates continued
to be processed, avoiding FIFO head-of-line starvation. This is an
operational correctness fix, not Phase 2 acceptance evidence.

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
- The bounded forward process uses the same public-only Daily contract and
  provider settings at a 15-minute cadence. It writes only to
  `/opt/polymarket-crypto-threshold/data/phase2-forward.db`, has a 14-day hard
  bound, and never writes the completed source database or final backup.
- The independent short-Up/Down process has the same no-secret/no-proxy safety
  boundary, disables Binance streaming, uses Polymarket's public crypto-window
  REST endpoint plus the public Chainlink reference stream, and writes only to
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
- Short-window model inputs persist Polymarket's public crypto-window
  `openPrice`, the current Chainlink tick, and the trailing volatility window
  before the signal. RTDS `boundary_tick()` is not a model input. Missing or
  malformed public window data is a hard rejection; Binance is never
  substituted for a Chainlink contract.
- Short-window net EV still uses token-specific REST books, full ask-depth
  VWAP, and the per-market fee schedule. Stream BBO is only a reprice hint.
- Chainlink settlement labels require Polymarket's completed public window
  `openPrice`/`closePrice`, Gamma's public `priceToBeat`/`finalPrice`, and the
  final Up/Down outcome to agree. Equality resolves to Up. Incomplete
  resolution metadata or window data remains pending and is retried.
- Replay manifests are explicitly family-scoped. Daily Binance and short
  Chainlink labels cannot be mixed, and a signal boundary must match the
  authoritative settlement boundary before entering replay.
- Versioned SQLite initialization/migration, foreign keys, WAL, transactions,
  source/model versions, and observed/received timestamps.
- Schema v5 durable settlement scheduling records each market's attempt count,
  status, next eligible retry, reason, and last resolution payload. Pending
  resolutions use bounded backoff (`5m`, `15m`, `1h`, `6h`); isolated errors
  use a one-hour retry. Candidate selection ranks due rows within separate
  retry and never-attempted groups, then interleaves the groups so one
  incomplete market cannot starve later markets and new candidates cannot
  permanently starve retries.
- Settlement resolution payloads use a settlement-semantic fingerprint before
  insertion. Repeated Gamma bodies do not create new `external_payloads` rows;
  existing historical duplicates are retained for audit and are not compacted
  by this fix.
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
- The acceptance schema gate explicitly supports historical v3/v4 and current
  v5 evidence. Tables are required from the version in which they were
  introduced; v5 still requires `settlement_attempts`, and unknown versions
  fail closed. A later collection gap does not erase a previously completed
  continuous 72-hour segment, while sparse endpoints still cannot manufacture
  such a segment.
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
markets, and short windows whose authoritative public `openPrice` is unavailable
or malformed are rejected.

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
- The direct-connect VPS shadow window completed with 72.956
  process-attributable hours and passed the mechanical continuity threshold.
- Valid closed Binance 1m stream ticks and reconnect generation were persisted
  on the deployment network. REST remained independently operational.
- Reconfirm that no order, fill, position, signer, or authenticated trading
  mutation exists, and that any configured private key remains Keychain-only
  and disconnected.

Until all items are evidenced, Phase 2 is **implemented but not accepted**.
An exit code `0` from the mechanical database checker still requires final
project review and never authorizes live capital or Phase 3 trading work.

## Verification Evidence

- Settlement scheduler follow-up commit `8866fb2` added the long-running
  retry/new rotation test. The final local verification for this change was
  `217 passed`; Ruff reported no findings, mypy reported no issues in 52
  source files, and `git diff --check` was clean.
- The local authoritative-boundary directed gate on 2026-07-27 passed 50
  Up/Down, public-client, settlement-scheduler, and CLI tests. It verifies that
  a missed RTDS boundary cannot become a model input, the public endpoint
  preserves decimal JSON values, malformed authoritative data rejects before
  probability/EV, settlement requires both public sources to agree, and a
  single cross-source IEEE-754 ULP representation difference does not weaken
  exact replay equality.
- The full local boundary-fix gate on 2026-07-27 passed `229` tests in
  `7.60s`; Ruff reported no findings, mypy reported no issues in 53 source
  files, and `git diff --check` was clean. No VPS process, service, database,
  deployment, or authenticated/trading mutation was performed.
- Before activation, the existing backup unit created the WAL-consistent
  `crypto-threshold-20260727T114109.997967Z.db` backup (`2,280,423,424`
  bytes) with `Result=success` and `ExecMainStatus=0`. The public-only network
  `doctor` then passed schema v5/WAL/foreign keys, Gamma discovery, CLOB time,
  Polymarket `authoritative_window_price_read`, all seven Chainlink pairs, and
  live NO-GO. Only the Up/Down unit was restarted. Its second v2 cycle linked
  all 14 latest signals to authoritative window payloads; the first three
  post-fix cycles completed through REST fallback with no forbidden tables.
  By `2026-07-27T11:51:36Z`, the latest full cycle had all 14 markets analyzed
  after the expected in-memory volatility warm-up and recorded four
  hypothetical paper entries.
- The v2 settlement path has persisted 13 labels while draining historical
  pending markets. Every label points to a `polymarket_site` /
  `authoritative_window_price` payload and all v2 label outcomes recompute
  without mismatch. These are not yet same-market pairs with post-fix v2
  signals: the first eligible analyzed 5m window ended at `11:55Z`, Gamma
  marked it closed but had not yet published `priceToBeat`/`finalPrice`.
  Therefore `post_fix_boundary_pair_market_count=0` is still **PENDING**, not a
  vacuous boundary pass, and no short replay is claimed.
- A targeted public-only settlement attempt for analyzed BTC 5m market
  `3113700` correctly returned `SettlementPendingError` because Gamma had not
  resolved it. The final read-only recheck at approximately `12:02Z` showed
  all seven `11:55Z` events closed but still missing both `priceToBeat` and
  `finalPrice`; no label was fabricated. The automatic backoff remains the
  owner of the next attempt.
- Final activation health showed marker `0bae0b7`, Up/Down PID `136544`,
  `NRestarts=0`, no error-priority journal entries, and three latest
  `complete_rest_fallback` cycles with 14 discovered and 14 analyzed. Daily
  remained successfully inactive and forward remained active on PID `132082`.
  No authenticated endpoint, credential, order, fill, position, or real
  trading mutation was used.
- Current local gate on 2026-07-25: `212 passed`; Ruff reported no findings;
  mypy reported no issues in 52 source files; `git diff --check` was clean.
- The short-Up/Down directed suite covers all 7 assets across both intervals,
  official public SDK-only imports, normalization, bounded/coalesced history,
  reconnect, lifecycle, secret-safe health, Gamma tag discovery, Up/Down token
  mapping, parser mismatches, authoritative-boundary hard rejection,
  mid-window boundary reconstruction, exact-input persistence, Chainlink tie
  settlement, family-scoped replay, and one-cycle 14-market orchestration.
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
- The fresh formal database evidence begins at
  `2026-07-24T05:34:07.579835+00:00`. The current daily systemd process start
  is `2026-07-24T05:39:56 CST`, so the first 5m48s of database history
  predates that process and must not be attributed to the current service
  without separate provenance. After the bounded deployment-unit restart,
  three cycles remained continuous with no gap above 300 seconds. The active
  process uses a 73-hour wall-clock bound so the 180-second cadence can still
  produce at least 72 persisted hours.
- The synchronized VPS baseline has real BTCUSDT/ETHUSDT closed ticks with
  `received_at >= provider_timestamp`, Polymarket REST fallback, paper skips,
  and clean schema-drift evidence. Its mechanical report is correctly
  `PENDING/NOT ACCEPTED`: closed-tick evidence now passes, while replay,
  calibration, 72-hour coverage, and reconnect remain pending.
- On 2026-07-26 at `14:21:39 UTC`, after activating `8866fb2`, the Up/Down
  VPS process remained `active/running` at PID `118423` with zero restarts,
  while the daily process remained PID `49268` with zero restarts. At
  `14:26:40 UTC`, the Up/Down database reported schema v5, 175 pending
  attempts (147 at attempt 1 and 28 at attempt 2), 553 succeeded attempts,
  and five consecutive `complete_rest_fallback` cycles.
- At that same snapshot, 133 pending/error attempts were due for another
  retry. This is a visible backlog-capacity warning, not a claim of
  acceptance: the retry/new interleaving is progressing old attempts, but
  the incoming unresolved-market rate can exceed the reserved retry slots.
- The known incomplete market `3078822` remained at 1,345
  `chainlink_resolution_event` payload rows with last ID `184505` and last
  receipt `2026-07-26T13:12:45Z` throughout both post-activation observations.
  Overall payload growth continued for legitimate live books and price ticks,
  but this resolution payload did not grow from repeated unchanged responses.
- The service remains public, read-only, and shadow-only: no signer,
  authenticated reconciliation, BUY/SELL, order, fill, or position mutation
  was executed during activation or verification.
- A Grok report observed at `2026-07-26T15:07:44Z` was useful as a health
  report but was not cursor-bounded: its `max_received_at` was later than its
  own observation time, and it omitted the age of one `in_progress` attempt.
  It is not accepted as a time-bounded evidence snapshot. The monitoring guide
  now requires a read window, one read-only transaction, process-versus-marker
  distinction, and explicit DB-history-versus-service-start comparison.
- A later Grok snapshot at `2026-07-27T02:39:59Z` passed its read-window
  consistency check. Both services were active with zero restarts, fresh
  cycles, synchronized NTP, healthy scheduled backups, and no forbidden
  trading tables. The daily database contained 10 unique settlement labels,
  still below the required 30 prior labels. Its bounded process remains due to
  end naturally at approximately `2026-07-27T06:39:56Z`
  (`14:39:56 Asia/Shanghai`); it must not be restarted merely to inspect it.
- A direct read-only acceptance evaluation at approximately
  `2026-07-27T03:20Z` measured a 69.7679-hour database span over 1,294 cycles
  with no oversized gap or overlong cycle. The no-trading surface,
  REST-fallback/rejection/paper evidence, external schema-drift monitor, and
  Binance closed-tick/reconnect evidence passed. The result remained
  `PENDING/NOT ACCEPTED`: the original daily evidence database is schema v3
  while the current checker expects v5 and `settlement_attempts`, and it has no
  sealed VPS replay, complete chronological OOS calibration, or complete
  metrics. The coverage gate also had not yet reached 72 hours at that read.
- The bounded daily process completed naturally at `2026-07-27T06:39:58Z`
  after reporting 1,353 process-attributable cycles. Systemd moved it to
  `inactive/dead` at `06:40:00Z` with `Result=success`, exit status 0, and zero
  restarts. It must remain stopped. A final read-only query found 1,353 cycles
  from the current process start, spanning 72.956423 hours with a maximum
  start-to-start gap of 215.808 seconds and maximum cycle duration of 35.797
  seconds. The database includes two earlier provenance-separated cycles, so
  its total is 1,355 and its mechanical span is 73.06 hours.
- Final source inspection reported `PRAGMA integrity_check=ok`, zero foreign-key
  violations, no forbidden trading tables, and SHA-256
  `daacca7d531808c4e4ff828c479b817d9eed55530cdfb7f22cf62b3d0f3f2287`.
  The final mechanical result remains `PENDING/NOT ACCEPTED`. Shadow coverage,
  REST/rejection/paper evidence, schema-drift monitoring over 134,030 payloads,
  no-trading surface, and Binance stream/reconnect evidence passed. Schema
  compatibility, sealed replay, chronological OOS calibration, and metrics
  failed.
- The final database is approximately 2.85 GB. Its WAL-consistent final backup
  is
  `/opt/polymarket-crypto-threshold/backups/final/crypto-threshold-20260727T083224.030493Z.db`
  with SHA-256
  `14b765acf51475b071cf086a68773576cb50ab3d77d7af28cb75d5db69c9d20e`,
  mode `0600`, `integrity_check=ok`, zero foreign-key violations, and no
  row-count or key-cursor differences across the 16 source tables. The original
  source hash and mtime remained unchanged. The old static Daily backup timer
  is now disabled to avoid duplicating a frozen 2.85 GB file.
- Commits `c45fc0b` and `4af0727` added version-aware acceptance, continuous
  segment detection, and the bounded forward collector. Local verification was
  `224 passed`; Ruff reported no findings, mypy reported no issues in 52 source
  files, and `git diff --check` was clean.
- The final backup was copied byte-for-byte into
  `/opt/polymarket-crypto-threshold/data/phase2-forward.db`, then only that
  working copy was migrated to schema v5. All 16 existing table row counts
  remained identical and only the empty `settlement_attempts` table was added.
  `doctor` passed database, public Gamma/CLOB/Binance/Coinbase access, and live
  NO-GO. The forward unit started at `2026-07-27T08:47:32Z` on PID `132082`
  with zero restarts; its first cycle completed through REST fallback with
  20 discovered, 10 analyzed workflows, zero hypothetical paper enters, no
  forbidden tables, and 10 newly persisted labels for a total of 20.
- The Up/Down snapshot's reported `boundary_mismatch=83` means 83 repeated
  signal rows across nine unique settled markets, not 83 markets. The rows
  comprise 65 analyzed and 18 rejected decisions across BNB, DOGE, ETH, HYPE,
  SOL, and XRP 5m/15m contracts. For each affected market, the persisted
  pre-fix boundary input was a live Chainlink tick approximately one second after the
  exact window boundary; Gamma's authoritative `priceToBeat` differed by up to
  106.612262 ppm. Strict replay exclusion is therefore working as designed and
  must not be relaxed to make the check pass. This is a real short-Up/Down
  settlement-source alignment failure, but it does not retroactively merge
  into or invalidate the separate daily Phase 2 database.
- A read-only public comparison then queried Polymarket's crypto-window endpoint
  for all nine affected markets. Its `openPrice` matched the final Gamma
  `priceToBeat` exactly for eight markets; the ETH 15m pair differed by one
  adjacent IEEE-754 ULP in JSON representation. An active-window observation
  also showed that `openPrice` was available before the close and remained
  unchanged after completion. The implementation permits only this
  cross-source one-ULP representation case; signal-to-label replay equality
  remains exact and no ppm/time tolerance was added.
- The same Grok snapshot saw all seven latest 5m signals rejected while all
  seven 15m signals were analyzed. A later read-only check found seven analyzed
  signals for each interval without a restart, so that pattern was transient
  rather than a process failure. The growing Up/Down settlement retry backlog
  remains a separate capacity warning.
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
- Polymarket's public crypto-window endpoint is used by the website but is not
  presently covered by the published API reference. Its URL, response schema,
  or availability may change. The adapter is versioned, raw responses are
  persisted, schema drift is monitored, and missing/malformed values fail
  closed, but this remains an operational dependency.
- The nine historical mismatch markets and their 83 signal rows remain invalid
  for short replay. They are retained for audit and must never be relabeled or
  admitted by widening price/time tolerance.
- Cross-source Gamma/window comparisons allow exact float identity or one
  adjacent IEEE-754 ULP only to accommodate JSON serialization. Replay still
  requires exact persisted decimal boundary equality.
- The boundary correction is deployed and has fresh v2 signal/payload evidence,
  and the v2 settlement code is producing labels for historical pending
  markets. It does not yet have a same-market post-fix v2 signal/label pair or
  sealed short replay. Runtime activation and a zero mismatch over zero pairs
  are not settlement or model acceptance.
- A fresh short-Up/Down process also needs the configured trailing volatility
  history before it can analyze a boundary; warm-up rejections are persisted
  and cannot be treated as market/model failures.
- Schema v5 prevents new duplicate resolution rows for unchanged settlement
  meaning, but it does not remove the large historical duplicate payload
  backlog. Missing `finalPrice` remains a legitimate pending state and cannot
  be converted into a label by the scheduler.
- The current retry quota is deliberately bounded to preserve new-candidate
  coverage. If unresolved markets arrive faster than retry capacity, the
  pending/error due backlog can grow even while every group receives service;
  this needs capacity policy and monitoring before any stronger claim.
- Gamma's 5m/15m discovery metadata can report misleading recurrence values;
  discovery therefore verifies series slug and exact window duration.
- Stream Market Channel data is BBO-only acceleration, not L2 executable depth;
  REST remains mandatory for VWAP and final validation.
- The official `polymarket-client` release is a beta API and can change.
- Public Binance WebSocket connectivity on the local development network
  requires the explicit local proxy. Direct VPS connectivity and reconnect
  evidence are verified. The stream remains opt-in and REST remains
  authoritative.
- Coinbase USD versus Binance USDT is only a sanity comparison, not the
  settlement source.
- The local sealed replay covers five unique settlement labels; the immutable
  VPS source has 10 labels and its forward working copy currently has 20, but
  neither VPS artifact yet has a sealed replay or valid out-of-sample
  calibration window.
- API schemas and fee behavior can change; parser and adapter versions make
  resulting records auditable but do not remove schema-drift risk.

## Next Gate

Keep the bounded forward collector running until at least 30 unique Daily
labels exist. At that point freeze and record the training cutoff, build and
verify the training replay, and do not use later labels to refit that window.
Continue collecting a separate later OOS decision batch, then rebuild the
combined chronological replay and publish raw, calibrated, and market-baseline
metrics. Repeating the five-hour local smoke or the completed continuous 72-hour
run is unnecessary. Live order placement remains explicitly outside this phase
and requires a separate design and approval.

In parallel, keep the short-Up/Down evidence in its separate database. The
authoritative-boundary correction is deployed without weakening replay
equality, and fresh `market-workflow-v2` signals now link to
`authoritative_window_price`. Let the configured volatility history warm up,
then wait for at least one same-market
`chainlink-polymarket-crypto-price-settlement-v2` label. Require a non-zero v2
pair count and zero exact v2 signal-to-label mismatch before building a new
`short_updown` replay. The historical 83 mismatched rows and historical pending
labels cannot satisfy that gate. Do not merge this evidence into the daily
Phase 2 acceptance run.

For ongoing VPS observation, check `settlement_attempts` in the Up/Down
database. A pending row is healthy when its `next_attempt_at` is in the
future; a growing due backlog, repeated attempt errors, or resolution payload
IDs advancing for an unchanged semantic response requires review. The
monitoring guide is read-only and must not repair or compact the backlog.

The formal Daily source is stopped, backed up, and reviewed. The old local smoke
history is now eligible for owner-approved deletion; it is no longer needed to
protect the completed VPS evidence.
