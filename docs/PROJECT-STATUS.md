# Project Status

**Date:** 2026-07-30

**Classification:** Research prototype

**Live status:** **NO-GO**

## Current Truth

Phase 0 truth repair, the Phase 1 read-only research loop, and the Phase 2
software path are implemented. The system can discover, analyze, label, replay,
calibrate, and paper-monitor real public Polymarket markets, but it is not a
trading bot and is not approved for live capital.

Phase 2 read-only research evidence closure is now mechanically accepted. The
local five-hour smoke
completed cleanly and produced a verified replay, but its 132 decision snapshots
represent only five unique settlement labels and no later out-of-sample window.
The current local development network requires an explicit outbound proxy for
Binance WebSocket access; the proxied stream produced valid BTCUSDT and ETHUSDT
1m Close ticks. The separate direct-connect Hong Kong VPS deployment completed
its formal bounded Phase 2 shadow window against a fresh evidence database on
2026-07-27. Continuous coverage and reconnect evidence passed. Its immutable
source contains 10 unique settlement labels. A separate v5 working copy then
continued collecting forward evidence. On 2026-07-28, its read-only
`replay-plan` reached `3922` eligible items and `34/30` replay-eligible unique
labels. A WAL-consistent independent snapshot was created, and the earliest 30
labels were sealed into a `3359`-item frozen training replay that verified
`3359/3359`. A later independent snapshot reproduced the exact frozen manifest,
added five valid post-cutoff OOS labels, sealed a `3984`-item combined replay
that verified `3984/3984`, and completed fixed-holdout calibration. The
mechanical checker returned `ACCEPTED` with all nine gates passing.

This is evidence acceptance, not a positive model result. All five OOS labels
belong to one BTC ladder and one settlement timestamp, so they are highly
correlated. Polymarket midpoint beat both the raw and calibrated probabilities
on Brier score, log loss, and ECE; calibration made this small sample worse.
The project remains a research prototype and real-capital trading remains
**NO-GO**. The owner has now explicitly prioritized the shortest defensible path
to a profitable live system and authorized direct VPS shadow/paper deployment
of a CEX-kline strategy. This authorization does not yet enable signing,
authenticated reconciliation, or real BUY/SELL.

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

Commit `0bae0b7` attempted to fix the Up/Down boundary contract and was
activated on the VPS at `2026-07-27T19:45:40+08:00`. A fresh strict read-only
audit on 2026-07-29 falsified its key assumption: during an active window the
public crypto-window response reports `completed=false`, `incomplete=true`,
and a provisional `openPrice`; after resolution, the completed response can
publish a materially different `openPrice`. The deployed
`market-workflow-v2` cached that provisional value. Exact signal/label
comparison found 28 mismatched v2 signal rows across seven markets, with zero
outcome-label mismatches. The older v1 history still contains 83 mismatched
rows across nine markets. Neither version is valid short replay evidence.

The 2026-07-29 audit observed deployment marker `52bc4df`, Forward active as
PID `180886`, and Up/Down active as PID `152162`, both with zero automatic
restarts. It also found 4,790 authoritative short labels across seven assets,
two intervals, and targets from 2026-07-25 through 2026-07-28. Those labels are
valid supervised targets even though v1/v2 pre-decision boundary inputs are not.

Workflow v4 now implements the intended strategy directly: Binance spot
one-minute candles closed at a fixed `T-60s` checkpoint predict the final
Chainlink Up/Down outcome. Chainlink's opening and closing values are never
pre-decision model inputs. Training fits only a chronological prefix and seals
the final 25% as a time holdout; runtime records the exact candles and
tamper-evident model artifact before comparing conservative probability bounds
with real target-size CLOB ask VWAP and fees. BTC, ETH, SOL, XRP, DOGE, and BNB
have verified Binance spot pairs. HYPE fails closed because Binance currently
has no `HYPEUSDT` spot pair.

Commit `c305d62` is deployed on the VPS and activated for Up/Down only. The
sealed artifact
`cex-kline-chainlink-direction-v1+49093373ec3e` trained on 3,151 chronological
samples and evaluated once on a later 984-sample holdout. Holdout Brier was
`0.114868` versus constant-baseline `0.249566`; log loss was `0.364537` versus
`0.692280`; accuracy was `84.4512%` versus `53.6585%`; ECE was `0.037817`.
These figures establish a historical directional signal, not a trading-profit
claim.

The first live shadow checkpoint produced workflow-v4 signals and hypothetical
paper entries. A follow-up audit exposed a paper-only settlement starvation
bug: 5,278 historical entries were open, 5,266 were older than the first v4
entry, and the old code applied its 1,000-row limit before filtering for
available labels. Commit `02fdb72` now joins each open entry to the exact label
for its signal deadline and contract family before applying the limit.

At the `2026-07-29T05:03:13Z` read-only snapshot, the database contained 78 v4
signals across 36 markets and four target times: 41 analyzed and 37 rejected,
primarily on incomplete/empty executable ask books. The signals had 541 exact
links across the seven input roles; every fully analyzed signal had all seven,
while preflight-expired rejections retained only the inputs read before the
fail-closed decision. No raw payload arrived after its signal timestamp, and no
signal contained a fabricated threshold. Three independent
authoritative Chainlink labels had naturally settled. Two v4 paper entries had
settled, both incorrectly predicted direction, for aggregate hypothetical PnL
of `-21.263620 USDC`; ten remained open. The historical labeled-open backlog
was zero after the fix. No order/fill/trade table exists.

Up/Down is active as PID `192194` with zero automatic restarts. Forward was not
restarted and remains active as PID `180886` with zero automatic restarts. The
deployment marker is `02fdb728ee4f25d02c71a2c5a5ab1c4080025940`.

Settlement remains independent of prediction and still requires the completed
window `openPrice`/`closePrice`, Gamma `priceToBeat`/`finalPrice`, and resolved
outcome to agree.

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
  boundary. Prediction reads public Binance REST klines and deliberately
  disables both Binance and Chainlink reference streams. Chainlink is read only
  after the deadline through the public completed-window settlement path. The
  process writes only to
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
- Short workflow v4 uses a sealed logistic model with a fixed `T-60s`
  checkpoint. Inputs are closed Binance one-minute candles only: window/short
  momentum, VWAP deviation, moving-average spread, RSI, realized volatility,
  range, candle body/wicks, volume, interval, and asset indicators. Future
  candles and final Chainlink values are mechanically excluded.
- Training reads authoritative Chainlink outcomes, splits by target timestamp,
  fits only the earliest 75%, and refuses to seal unless the final 25% holdout
  beats a constant training-rate baseline on Brier and log loss with accuracy
  above chance. Artifact hashes bind features, coefficients, cutoff, metrics,
  and dataset identity.
- Short-window net EV still uses token-specific REST books, full ask-depth
  VWAP, and the per-market fee schedule. Stream BBO is only a reprice hint.
- Chainlink settlement labels require Polymarket's completed public window
  `openPrice`/`closePrice`, Gamma's public `priceToBeat`/`finalPrice`, and the
  final Up/Down outcome to agree. Equality resolves to Up. Incomplete
  resolution metadata or window data remains pending and is retried.
- Replay manifests are explicitly family-scoped. Daily Binance and short
  Chainlink labels cannot be mixed. New short replay candidates must use
  `market-workflow-v4`, have no fabricated strike, and include exact CEX candle
  plus sealed-model inputs. Historical v1/v2/v3 signals remain retained for
  audit but are ineligible.
- Versioned SQLite initialization/migration, foreign keys, WAL, transactions,
  source/model versions, and observed/received timestamps.
- Schema v5 durable settlement scheduling records each market's attempt count,
  status, next eligible retry, reason, and last resolution payload. Pending
  resolutions use bounded backoff (`5m`, `15m`, `1h`, `6h`); isolated errors
  use a one-hour retry. Candidate selection ranks due rows within separate
  retry and never-attempted groups, then interleaves the groups so one
  incomplete market cannot starve later markets and new candidates cannot
  permanently starve retries. The shadow monitor has a separate settlement
  batch limit instead of coupling settlement throughput to the analysis limit;
  short automatic candidates additionally require an analyzed v4 pre-deadline
  signal.
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
- Replay manifest v3 can freeze the earliest N eligible labels and bind a later
  combined replay to that exact training dataset, manifest hash, label list,
  cutoff, and item identities. Dataset hashes now include each item's
  `decision_at` and `label_available_at`; verification remains compatible with
  legacy v1/v2 manifests. Fixed-holdout calibration v3 reads training samples
  only from the frozen replay. Later OOS labels are evaluated only after every
  frozen training label was strictly available and are never added back into
  the histogram. Repeated snapshots remain in replay for audit but cannot
  impersonate independent labels. Reports include Brier score, log loss, and
  ECE for raw, calibrated, and Polymarket midpoint-baseline probabilities.
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
-> frozen-training OOS calibration -> stream-triggered REST shadow
-> persistent paper ledger
```

The acceptance criteria are:

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

All items are now evidenced on the independent accepted snapshot. The
mechanical checker returned exit code `0`, and final project review accepts
Phase 2 as a complete read-only research and evidence pipeline. The observed
five-label OOS result does not show model improvement over Polymarket and is too
correlated for a profitability claim. Phase 2 acceptance never authorizes live
capital or Phase 3 trading work.

## Verification Evidence

- CEX-direction deployment commit `c305d62` passed `253` tests; Ruff reported no
  findings; mypy reported no issues in 54 source files; and
  `git diff --check` was clean. Its source archive SHA-256 was
  `1c2d9df4b3188f8017f69dd5376bf64be05a11bf7a87edf6036efd1fca8191cb`
  both locally and on the VPS before extraction. Public-only `doctor` passed
  before the Up/Down-only restart. Forward retained PID `180886`; Up/Down moved
  from PID `152162` to `190355`; both report `NRestarts=0`.
- The deployed v4 audit at `2026-07-29T04:52:17Z` found 42 signals, 294 exact
  input links across seven required roles, zero raw-after-signal violations,
  zero non-null thresholds, nine hypothetical entries, and one naturally
  completed authoritative label. Seventeen signals rejected on empty or
  incomplete REST ask books; this is expected fail-closed behavior, not a
  synthetic probability or paper fill.
- Follow-up commit `02fdb72` fixed paper settlement starvation by selecting
  only exact signal/label pairs before the 1,000-row batch limit. Its regression
  test proves a newer labeled entry settles even when an older unlabeled entry
  is first in the ledger. The complete gate passed `254` tests; Ruff, mypy
  across 54 source files, and `git diff --check` passed. The deployed archive
  SHA-256 was
  `c93037bc75c5424033be341e8d5eae482e8b229d1b5df98ae013cc5284637eef`.
  After the Up/Down-only restart, two v4 entries settled immediately and the
  labeled-open historical backlog fell from 2,051 to zero. Both first v4
  entries lost, for aggregate hypothetical PnL `-21.263620 USDC`; this result
  is retained rather than filtered or rerun.
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
- A strict read-only forward audit at `2026-07-27T12:27Z` found 31 immutable
  Daily labels across 31 markets, but only 25 labels had at least one matching
  analyzed signal with the required replay fields. Those 25 labels were split
  9/8/8 across the July 24/25/26 target batches and linked to 2,572 candidate
  signal snapshots. Six historical labels had no eligible analyzed decision
  and cannot be counted or fabricated into training evidence. No replay was
  created during this audit.
- Replay manifest v3 retains the v2 fail-closed training selection boundary
  and binds replay-item decision/label-availability timestamps into the dataset
  hash.
  `replay-plan --db <snapshot-or-source.db> --training-label-count 30` performs
  the exact candidate/input validation through a read-only SQLite connection
  and returns `PENDING` without writes until 30 eligible unique labels exist.
  `replay-build --training-label-count 30` then selects the earliest labels by
  `(label_received_at, label_id)`, persists the exact label list and cutoff in
  the immutable manifest, and refuses to seal anything when fewer than 30 are
  eligible. Offline verification validates v2/v3 selection metadata and keeps
  legacy manifest compatibility.
- A later combined replay must be built with
  `replay-build --training-dataset <frozen-dataset>`. The combined manifest
  binds the frozen dataset hash, labels, cutoff, and exact item identities.
  Calibration v3 refuses an unbound replay and keeps the frozen histogram
  unchanged across every OOS label. Mechanical Phase 2 acceptance independently
  recomputes the fixed-window OOS count and rejects missing or invalid training
  references.
- Commit `4f34b8f` passed `235` tests, Ruff, mypy over 53 source files, and
  `git diff --check`, then was deployed as the VPS filesystem marker without
  restarting Forward or Up/Down. Their PIDs remained `132082` and `136544`,
  both active/running with zero restarts. The deployed exact read-only command
  returned `PENDING`, `eligible_items=2572`, and
  `eligible_unique_labels=25/30`; the forward DB still contained zero replay
  datasets and its latest observed cycle remained `complete_rest_fallback`.
- Commit `6002bc4` completed the frozen-training-to-combined-replay reference
  chain, fixed-holdout calibration v3, strict all-training-label availability,
  replay-item timestamp hashing, and independent acceptance recomputation. It
  passed `242` tests, Ruff, mypy over 53 source files, and `git diff --check`,
  then was deployed at `2026-07-27T14:21Z` without restarting either service.
  The marker and newly imported source versions were `6002bc4`,
  `replay-manifest-v3`, and `fixed-holdout-calibration-v3`; PIDs remained
  `132082` and `136544` with zero restarts. The exact read-only plan remained
  `PENDING` at `2572` eligible items and `25/30` eligible unique labels.
  Forward still had zero replay datasets, zero calibration runs, no forbidden
  trading tables, and a latest `complete_rest_fallback` cycle with 20
  discovered, 10 analyzed, and zero hypothetical paper entries.
- On 2026-07-28, the exact read-only plan advanced to `3922` eligible items and
  `34/30` eligible unique labels. SQLite `backup()` created the independent
  training snapshot
  `/opt/polymarket-crypto-threshold/backups/phase2-training-20260728/crypto-threshold-20260728T101754.978295Z.db`.
  Before replay construction it had SHA-256
  `3c9e98a2d5f5d966c971210dfcaae9a81d36739311b42f42f16e1f528aa521e7`,
  `integrity_check=ok`, zero foreign-key violations, schema v5, and zero replay
  or calibration rows. The frozen dataset
  `replay:204eca70-c64a-4539-9bde-2f504f5f68af` selected the earliest 30
  labels, sealed `3359` items, and produced manifest SHA-256
  `da7b245a89cd1e03f2d25137f1aa86da791ecc4f0cf0fff4df0928cfa03a370e`.
  Its exact cutoff is `2026-07-27T16:13:16.984047+00:00` at
  `label:88dce179-e282-434a-9323-cdcbb08f8b30`, and offline verification
  passed `3359/3359`. The post-build database SHA-256 is
  `ff1d594d2608daf42655bd4bd258f5dd0c6838479ee50930519ce5111de57b0c`;
  a second integrity check passed with zero foreign-key violations.
- The four non-training labels in that snapshot have selected decision times
  between `2026-07-26T23:23:32Z` and `2026-07-27T15:42:48Z`, all before the
  frozen cutoff, so the exact fixed-holdout audit reports
  `valid_oos_labels=0`. No combined replay or calibration run was created.
  The actively collected source database remained at zero replay datasets,
  replay items, and calibration runs. It already contains 37 analyzed
  post-cutoff decisions across three Daily markets with deadline
  `2026-07-28T16:00:00Z`; these may become the first valid OOS labels only
  after authoritative settlement. At the final observation the forward unit
  was `active/running`, PID `152184`, with zero restarts and a latest
  `complete_rest_fallback` cycle.
- On 2026-07-29, five new labels passed the strict OOS chronology check. A new
  WAL-consistent snapshot was created at
  `/opt/polymarket-crypto-threshold/backups/phase2-oos-20260729/crypto-threshold-20260728T173136.176463Z.db`.
  Its pre-build SHA-256 was
  `3d81adf371605c364d486dd33995a51ae58f1029cb72ab8f4cd776baa881b5e0`;
  it began at schema v5 with `integrity_check=ok`, zero foreign-key violations,
  and zero replay or calibration rows. Rebuilding the 30-label training replay
  produced the exact original `3359` items, cutoff, selected labels, and
  manifest SHA-256
  `da7b245a89cd1e03f2d25137f1aa86da791ecc4f0cf0fff4df0928cfa03a370e`,
  then verified `3359/3359`.
- The bound combined dataset
  `replay:d9adcb2c-fcec-49b2-b24b-7c793e4a5d39` sealed `3984` items across 39
  labels with manifest SHA-256
  `bb818edcf9b288687f08bcb22a93a852bd9ebe94ab3dfe3ea7512d032ed1c9ab`
  and verified `3984/3984`. Fixed-holdout run
  `calibration:dbc1dc3e-638d-4b2a-9688-b4531d4baf7a` used 30 frozen labels and
  evaluated five OOS labels without refitting. The mechanical acceptance
  command returned exit code `0` and all nine checks passed. The report
  SHA-256 is
  `c347dccdcb6a493046149d4b6f6bfd96e95108b25bd206e4d6fee5aaa1b8ca84`;
  the final evidence DB SHA-256 is
  `a2401aeae0a84e602ff52133864082477762ba3a021e13acd177fe9291300e8e`.
  Final read-only inspection reported `integrity_check=ok`, zero foreign-key
  violations, two replay datasets, `7343` replay items, one calibration run,
  and no forbidden trading tables. The active source remained at zero replay
  and calibration rows.
- The five OOS labels are one BTC ladder at the same
  `2026-07-28T16:00:00Z` settlement, with strikes from `$56,000` to `$64,000`;
  four resolved Yes and one resolved No at a common Binance close of
  `63926.66`. Lower is better for all reported metrics. Polymarket midpoint
  scored Brier `0.00420955`, log loss `0.03303304`, and ECE `0.0307`; the raw
  model scored `0.01123915`, `0.05491243`, and `0.0482096`; calibrated
  probabilities scored `0.05138889`, `0.17267713`, and `0.13333333`.
  Therefore this accepted evidence run does not show model improvement and
  supplies no profitability or live-readiness evidence.
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
- A read-only public comparison queried Polymarket's crypto-window endpoint for
  all nine original v1 mismatch markets. Its completed `openPrice` matched
  final Gamma `priceToBeat` exactly for eight markets; the ETH 15m pair differed
  by one adjacent IEEE-754 ULP in JSON representation. The earlier single
  active-window observation appeared stable, but the larger 2026-07-29 v2
  audit disproved that generalization: active provisional values changed by
  approximately 0.23 to 234.914 ppm by completion. The one-ULP allowance
  remains limited to completed cross-source settlement comparison; no
  price/time tolerance is allowed for signal-to-label replay.
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
- The deployed v2 boundary correction is not valid: 28 signal rows across seven
  labeled markets fail exact boundary equality. They remain permanently
  excluded. Workflow v4 removes the signal-boundary requirement instead of
  widening it: the target is direction, and CEX candles are the predictor.
- Only four days of historical labels are currently available. Even if the
  sealed one-day holdout is strong, it is model-validation evidence rather than
  proof of fee-adjusted profitability. Forward VPS paper outcomes must span
  materially different dates and regimes.
- HYPE remains unsupported by the first CEX model because Binance has no
  verified HYPE spot pair. It must reject before CLOB reads rather than silently
  use another asset or an unvalidated feed.
- Schema v5 prevents new duplicate resolution rows for unchanged settlement
  meaning, but it does not remove the large historical duplicate payload
  backlog. Missing `finalPrice` remains a legitimate pending state and cannot
  be converted into a label by the scheduler.
- The historical database still contains a large v1/v2 settlement-attempt
  backlog. Workflow v4 now uses the separately configured
  `SHADOW_SETTLEMENT_LIMIT=50`, and automatic short settlement requires a valid
  analyzed v4 signal. This fix is deployed and the first v4 label completed,
  but the historical attempt rows remain retained for audit.
- Eight historical `.partial-*` backup artifacts from 2026-07-24 remain on the
  VPS. Current backups no longer create them, and the cleanup helper already
  handles new temporary sidecars. Removing the historical files is a separate
  owner-approved remote mutation.
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
  bounded VPS source has 10 labels. The accepted forward snapshot has a frozen
  30-label training replay and five valid OOS labels, but all five OOS labels
  are strikes in one BTC ladder at one settlement timestamp. This is a valid
  chronology gate and a weak statistical sample. The market baseline beat both
  raw and calibrated probabilities, and calibration degraded every reported
  metric.
- API schemas and fee behavior can change; parser and adapter versions make
  resulting records auditable but do not remove schema-drift risk.

## Next Gate

Phase 2 read-only research acceptance is complete. The owner's primary product
objective is now explicit: use CEX price action to predict Chainlink-settled
Up/Down markets, prove executable fee-adjusted edge as quickly as forward data
allows, and then make a separate live-capital decision. Workflow v4 is the
deployed strategy path; Phase 2 interface checks are supporting infrastructure,
not the product objective.

The immediate gate is to keep the sealed v4 artifact unchanged while collecting
independent VPS signal/label/paper pairs across later windows and market
regimes. Report directional metrics and executable-price paper PnL separately:
high prediction accuracy alone does not prove a profitable entry price.
Repeatedly refitting the model on these forward outcomes is forbidden because
it would destroy the test. Real BUY/SELL, signing, and authenticated
reconciliation remain disabled until a separate live-capital authorization
defines capital, per-trade size, daily loss limit, and kill conditions.

The separate Daily forward collector remains available for a larger,
event-diverse OOS set across multiple settlement dates and supported Daily
assets. Repeating the five-hour local smoke or completed continuous 72-hour run
is unnecessary.

The predeclared event-diverse checkpoint is at least 20 distinct
`(asset, target_time_utc)` groups across at least seven settlement dates, with
BTC, ETH, SOL, and XRP all represented and at least four groups per asset.
Multiple strikes in one ladder count as one event group. The accepted five-label
BTC ladder therefore starts this checkpoint at `1/20` groups, `1/7` dates, and
one of four assets.

### Approved Strategy Research Roadmap (2026-07-30)

The owner approved the following research sequence. This roadmap is
additive to the sealed v4 artifact: it does not authorize refitting v4, live
BUY/SELL, signing, authenticated reconciliation, or a merge of Daily and
short-Up/Down evidence. New challenger data must use a separate database or
an explicitly versioned, family-scoped evidence namespace. Roadmap approval
alone did not implement a work package; the R0 implementation and its evidence
boundary are recorded below.

#### R0 — Challenger collection: T-180/T-120/T-60/T-30 + market baseline + latency replay

**Status:** **IMPLEMENTED FOR SHADOW COLLECTION**

Activation and settled evidence must still be verified on the VPS.

Collect the same short-Up/Down markets at four pre-declared decision
checkpoints. At every checkpoint, persist:

- the sealed v4 probability as a frozen reference;
- a market baseline from Polymarket midpoint and executable target-size ask
  VWAP;
- the current fee schedule, spread, depth, and slippage;
- a fixed latency-sensitivity replay (zero-delay reference plus conservative
  100 ms, 250 ms, 500 ms, and 1 s scenarios);
- the authoritative settlement label when it later becomes available.

R0 is a challenger-collection and measurement task, not a v4 promotion.
Its minimum report must separate Brier/log loss/ECE, market-baseline error,
fee-adjusted net EV, assumed fill rate, and paper drawdown/capital lock. A
directional accuracy improvement without a positive executable edge does not
pass.

Implementation uses schema v6 and the isolated
`short_challenger_observations` / `short_latency_replays` tables. The scheduler
opens non-overlapping T-180/T-120/T-60/T-30 decision bands, applies the sealed
v4 artifact without refitting, and retains midpoint, target-size ask VWAP, fee,
spread, depth, and slippage at every checkpoint. For a selected paper side it
performs fresh public CLOB REST reads at requested 0/100/250/500/1000 ms
delays, stores actual observed delay, and fails closed on stale, incomplete, or
timestamp-untrusted books. Only the T-60 signal continues into the legacy
paper ledger; all four counterfactuals stay in the challenger namespace.
Authoritative labels later settle challenger rows through the exact
market/target/family join. Implementation and green tests do not count as
forward evidence; the first complete VPS checkpoint grid is still required.

#### R1 — HFTBacktest-inspired microstructure and execution replay

**Status:** **PLANNED — depends on R0 data**

Use the HFTBacktest design as the reference for tick-by-tick replay, feed and
order latency, order-book depth, queue position, and fill-model sensitivity.
R0's latency grid is only a coarse snapshot sensitivity check; R1 is the
full-tick/L2 replay and queue-model hardening step. The first implementation
should remain a small local/research component rather than importing a
production trading engine. Candidate CEX inputs are L2 imbalance,
micro-price/VAMP, aggressive trade imbalance, spot-perpetual basis, and
cross-venue lead/lag.

R1 passes only when the replay is conservative across more than one fill and
latency assumption and does not depend on an optimistic queue model. It must
also explain which part of any profit comes from prediction, spread capture,
rebates, or residual directional inventory.

#### R2 — Freqtrade-inspired research-integrity gates

**Status:** **PLANNED — required before factor promotion**

Add explicit automated checks for look-ahead bias, recursive/rolling-feature
contamination, timestamp gaps, future payloads, and accidental reuse of
holdout outcomes. Add grouped chronological validation with a purge/embargo
window for overlapping 5-minute and 15-minute events. Keep dry-run/paper
behavior mechanically separate from any authenticated execution surface.

R2 passes only with zero detected future-input violations and a reproducible
sealed manifest. A green unit-test run alone is not an R2 pass.

#### R3 — VectorBT-inspired offline factor screening

**Status:** **PLANNED — after R1 and R2**

Use fast offline sweeps to test narrowly defined factor families and
checkpoint/threshold combinations. Each experiment must declare its search
space before looking at its OOS result, retain losing trials, and compare
against the market baseline and the frozen v4 reference. Automated feature
generation is a discovery aid, never an automatic production promotion path.

R3 passes only when a factor family shows positive fee/slippage-adjusted
out-of-sample edge across independent dates, assets, and regimes, with
stability under the R1 latency/fill replay.

#### R4 — NautilusTrader-inspired execution-layer spike

**Status:** **PLANNED — last, only after positive executable evidence**

Study and, if justified, prototype an isolated adapter boundary using
NautilusTrader's deterministic event model and its documented Polymarket order
semantics. The spike must cover FAK/FOK/GTC/GTD behavior, quote-versus-token
quantity, ambiguous submit outcomes, cancellation, and reconciliation. It
must be pinned to a reviewed stable revision; a fast-moving development or
release-candidate dependency is not production approval.

R4 is an execution-engineering milestone, not a strategy-evidence shortcut.
It remains read-only/no-secret until a separate live-capital authorization
specifies capital, per-trade size, daily loss limit, and kill conditions.

The implementation order is therefore:

```text
R0 challenger measurements
  -> R2 integrity gates
  -> R1 microstructure/execution replay hardening
  -> R3 factor screening
  -> R4 isolated execution-layer spike
```

R1 may begin collecting raw CEX microstructure data in parallel with R0, but
no factor or execution result may be promoted until R2 has passed. The sealed
v4 artifact, the current VPS services, and their no-trading boundaries remain
unchanged throughout this roadmap.

Commit `52bc4df` replaced relevance-only Daily discovery with an explicit
America/New_York settlement-date query and round-robin BTC/ETH/SOL/XRP
selection. It was deployed to the direct-connect VPS and activated for Forward
only on 2026-07-29; Up/Down was not restarted. The first 10-of-20 cycle proved
all four assets were persisted but also showed that the fixed analysis cap
selected only the low end of each ladder: nine signals were safely rejected for
empty or stale books and only one BTC signal was complete. A read-only CLOB
probe found executable books in the unselected half, so the Forward-only
configuration now analyzes all 20 discovered markets every 15 minutes.

The first 20-of-20 cycle, `shadow:c679c0c3-6143-4281-b43a-f068ca093046`,
completed at `2026-07-28T18:13:21.779328+00:00` with no cycle reasons. Its
`analyzed`/`rejected` counts were BTC `3/2`, ETH `2/3`, SOL `1/4`, and XRP
`3/2`, all for `2026-07-29T16:00:00+00:00`. Every signal linked eight raw
payloads, the raw-before-signal violation count was zero, forbidden trading
tables were absent, and the only paper entry was hypothetical. These four
asset-date groups are prospective and do not count toward the checkpoint until
valid settlement labels exist. The completed checkpoint therefore remains
`1/20` groups, `1/7` dates, and one of four assets. The controlled configuration
restart reset the 336-hour Forward bound to approximately
`2026-08-12T02:12:41+08:00`.

The 2026-07-29 read-only follow-up found later Forward cycles reporting
`20 discovered / 18 analyzed`. Two SOL range contracts were correctly rejected
as unsupported but had already consumed discovery slots. The local monitor now
requests up to twice the Daily candidate budget, persists all raw/rule evidence,
then selects only supported rules with asset-balanced round-robin until the
configured 20-slot budget is full. This change is present in the deployed
source tree but has not been activated in Forward; the running Forward process
remains untouched.

Keep short-Up/Down evidence in its separate database. Never build a v1/v2/v3
short replay. Workflow v4 is trained, sealed, deployed with
`TRADING_DISABLED=true`, and producing new forward evidence. Count only its new
signal/label pairs, do not refit the artifact on them, and do not merge short
evidence into Daily Phase 2. Compare model probabilities with executable CLOB
prices and report conservative paper PnL after fees; a high prediction accuracy
without a price edge is not a profit result.

For ongoing VPS observation, check `settlement_attempts` in the Up/Down
database. A pending row is healthy when its `next_attempt_at` is in the
future; a growing due backlog, repeated attempt errors, or resolution payload
IDs advancing for an unchanged semantic response requires review. The
monitoring guide is read-only and must not repair or compact the backlog. The
owner authorized and the project completed deployment plus an Up/Down-only
restart for this strategy. Forward must not be restarted without a separate
operational reason.

The formal Daily source is stopped, backed up, and reviewed. The old local smoke
history is now eligible for owner-approved deletion; it is no longer needed to
protect the completed VPS evidence.
