# CEX Kline → Chainlink Up/Down Strategy

**Status:** shadow/paper only
**Real orders:** disabled
**Runtime family:** `short_updown`

## Objective

Predict whether Polymarket's authoritative Chainlink close will be greater than
or equal to its opening value for each 5-minute or 15-minute window. The
prediction input is public CEX market data; Chainlink is the target and final
settlement truth, not a pre-decision feature.

The first deployed strategy uses Binance spot one-minute candles for BTC, ETH,
SOL, XRP, DOGE, and BNB. Binance does not currently expose a `HYPEUSDT` spot
market, so HYPE fails closed until a separately validated CEX candle source is
implemented.

## Exact Decision Contract

For a contract ending at `T`:

1. The sealed v4 reference and its legacy paper ledger remain fixed at
   `T-60s`.
2. R0 evaluates that same frozen artifact at the separately declared
   `T-180s`, `T-120s`, `T-60s`, and `T-30s` checkpoints. It does not refit or
   recalibrate the artifact.
3. Only one-minute candles with `close_time <= checkpoint` are eligible. At
   `T-30s`, the newest eligible one-minute candle normally closes at `T-60s`;
   no partial candle is used.
4. The candle opened at the Chainlink window start must exist.
5. The most recent 21 closed candles must be contiguous.
6. The final Chainlink open, close, Gamma result, and any data received after
   the checkpoint are forbidden model inputs.
7. Runtime analysis is allowed only inside the checkpoint's declared
   non-overlapping decision band. The legacy T-60 path stops ten seconds
   before settlement; the isolated R0 challenger path stops five seconds
   before settlement. A completed analysis at or after `T` is rejected.

The legacy T-60 reference is a late-window nowcast: for a 5-minute market it
observes the first four closed one-minute candles and predicts the final
Chainlink direction; for a 15-minute market it observes the first fourteen.
R0 deliberately measures that unchanged model earlier and at T-30 without
claiming that its original calibration transfers across checkpoints.

## Features and Model

The sealed logistic model uses:

- return since the contract window opened;
- 1m, 3m, and 5m log returns;
- SMA(3) versus SMA(10);
- 15-minute VWAP deviation;
- RSI(14);
- ten-return realized volatility;
- five-candle normalized range;
- latest candle body and wick skew;
- 20-candle volume z-score;
- interval and asset indicators.

Training reads existing immutable `short_updown` Chainlink settlement labels.
It splits by target timestamp, fits only the earliest 75% of timestamps, and
never refits on the final 25% holdout. The command refuses to seal an artifact
unless holdout Brier and log loss both beat a constant training-rate baseline
and holdout accuracy is above chance. The artifact contains its feature
manifest, coefficients, training cutoff, metrics, dataset hash, and a
tamper-evident artifact hash.

```bash
crypto-threshold train-short-cex \
  --db /opt/polymarket-crypto-threshold/data/updown-shadow.db \
  --output /opt/polymarket-crypto-threshold/data/models/cex-direction-v1.json
```

## Runtime and Paper Policy

For every due market the canonical workflow records, in order:

1. Gamma market identity and settlement rule;
2. public Up and Down REST order books;
3. the public market fee schedule;
4. the exact closed Binance candles;
5. the sealed model artifact;
6. the probability and real target-size ask VWAP for both outcomes.

For shadow entry selection, the model probability is widened by a
pre-declared fixed five-percentage-point margin; the holdout does not tune this
value. YES uses the lower probability bound and NO uses `1 - upper_bound`. Net
EV then subtracts the actual target-size ask VWAP and the published taker fee.
A paper entry requires conservative net EV of at least 2%. The ledger is
hypothetical and never calls an authenticated client.

When `SHORT_CHALLENGER_ENABLED=true`, R0 writes to the separate
`short_challenger_observations` and `short_latency_replays` tables. Every
checkpoint stores the frozen probability, both Polymarket midpoints, executable
target-size ask VWAPs, fee, full spread, depth, and slippage. For a selected
paper side, the public REST book is fetched again at requested delays of
`0/100/250/500/1000 ms`; both requested and actual arrival delay are retained.
Incomplete, stale, or timestamp-untrusted books are stored as fail-closed
skips. These counterfactual rows settle only from the same authoritative label
join used by the short-Up/Down workflow and never enter the legacy T-60 ledger.

Collection status is available without opening the database for writes:

```bash
crypto-threshold short-challenger-status \
  --db /opt/polymarket-crypto-threshold/data/updown-shadow.db
```

Final settlement independently requires all of the following to agree:

- the completed Polymarket Chainlink window open and close;
- Gamma `priceToBeat` and `finalPrice`;
- Gamma's resolved Up/Down outcome.

## Deployed Snapshot

Strategy commit `c305d62` and paper-settlement fix `02fdb72` are active on the
VPS with
`TRADING_DISABLED=true`. The sealed artifact
`cex-kline-chainlink-direction-v1+49093373ec3e` used 3,151 chronological
training samples and a later 984-sample holdout. Holdout Brier/log loss were
`0.114868`/`0.364537`, versus constant-baseline
`0.249566`/`0.692280`; accuracy was `84.4512%` and ECE was `0.037817`.

At `2026-07-29T05:03:13Z`, the independent Up/Down database contained 78
workflow-v4 signals and 541 exact input links across seven input roles. Fully
analyzed signals had all seven; preflight-expired rejections retained only the
identity inputs read before rejection. Raw-after-signal violations remained
zero. The ledger held 12 hypothetical entries and three completed authoritative
v4 labels. Two entries had settled and both lost, for aggregate hypothetical
PnL of `-21.263620 USDC`. Empty executable books rejected safely. Forward
remained on its original PID and was not restarted.

The isolated R0 challenger is also active in schema v6. Commit `76d0ad0`
(`76d0ad0671bc0d8db8c86eeeac5a1e8f0470c251`) was activated for
`crypto-threshold-updown-shadow.service` at `2026-07-29T17:38:08Z`; Forward
was not restarted. At `2026-07-29T17:48:37Z`, the challenger had 156
observations across five target times and six assets, 780 latency replays, and
the complete declared latency grid. T-30 had 6 captured observations overall;
the post-fix slice had 3 captured and 15 fail-closed rejections, with no late
or expired-checkpoint reasons. Ten rows had settled, but their paper PnL is
hypothetical research evidence rather than verified trading profit.

## Open-source Research Used

No unlicensed project code was copied. The implementation uses standard
indicator formulas and these structural lessons:

- [Freqtrade](https://github.com/freqtrade/freqtrade): dry-run first,
  chronological backtests, and explicit lookahead analysis.
- [polymarket-kalshi-weather-bot](https://github.com/suislanchez/polymarket-kalshi-weather-bot):
  one-minute multi-CEX candles, momentum, RSI, VWAP, trend, and market-price
  edge gating.
- [polymarket-bot-arena](https://github.com/ThinkEnigmatic/polymarket-bot-arena):
  separate momentum/mean-reversion hypotheses and forward scorekeeping.
- [polymarket-5min-15min-1hour-arbitrage-trading-bot](https://github.com/PolyBullLabs/polymarket-5min-15min-1hour-arbitrage-trading-bot):
  late-window entry, spread/confidence gates, and short-market timing.

Repository profit claims are not treated as evidence. Only this project's
sealed holdout and later VPS forward outcomes count.

The approved follow-up research and execution roadmap is maintained in the
single authoritative status document:
[PROJECT-STATUS.md](PROJECT-STATUS.md#approved-strategy-research-roadmap-2026-07-30).
This strategy note describes the current v4 baseline; it is not a second
source of roadmap status.

## Promotion Gate

Deployment to VPS shadow is authorized. Real BUY/SELL remains disabled until a
separate, explicit live-capital decision based on independent forward events,
fee/slippage-adjusted paper results, operational health, and loss limits.
