# R3 Factor Screening Contract

R3 is a sealed, offline screening step. It is not a trading strategy and it
does not auto-promote a factor.

The implementation is
`crypto_threshold.services.factor_screening_service.FactorScreeningService`.
It accepts a sealed `FactorExperimentSpec` plus independent, settled
`FactorObservation` rows. The spec hash covers the cutoff, minimum coverage,
required assets, stake, model/baseline versions, integrity and replay
versions, and every declared rule. A changed spec is rejected.

## Required comparisons

Every trial is evaluated against all three frozen quantities:

- the candidate probability;
- the executable market baseline;
- the frozen v4 CEX-kline model.

The report records Brier score, log loss, ECE, triggered event groups, fill
ratio, fees, payout, total PnL, and fee-adjusted net EV per attempted stake.
Entry uses the token-specific executable price and the observed conservative
replay fill ratio; midpoint is not treated as a fill.

All observations must be strictly after the sealed training cutoff, have one
unique `(asset, target_time_utc)` event group, and carry both an integrity
manifest hash and an R1 replay manifest hash. Missing raw lineage, future
inputs, duplicate groups, invalid probabilities, or invalid fill ratios fail
closed. Failed trials remain in the report with explicit reasons.

The current pre-registered grid is created by the independent microstructure
shadow service. Its comparison model is pinned to the deployed artifact runtime
version `cex-kline-chainlink-direction-v1+49093373ec3e`, including the first
12 characters of the verified artifact hash:

| Rule | Factor | Condition | Side |
|---|---|---|---|
| `obi-positive-010` | `book_imbalance` | `> 0.10` | YES |
| `trade-positive-010` | `aggressive_trade_imbalance` | `> 0.10` | YES |
| `basis-negative-2bps` | `spot_perpetual_basis_bps` | `< -2` | NO |
| `btc-lead-positive-020` | `btc_lead_correlation` | `> 0.20` | YES |

The gate requires at least 20 independent event groups, seven settlement
dates, all of BTC/ETH/SOL/XRP, and four groups per asset. Until those settled
labels and complete replay/input manifests exist, the run remains
`preregistered_waiting_for_settled_oos`. `promotion_allowed` is always false
in this phase.

Run an offline envelope with a sealed spec and independent observations:

```bash
uv run crypto-threshold factor-screen \
  sealed-factor-observations.json \
  --output factor-screen-report.json
```

The JSON report is an audit artifact only. A positive screen is a prerequisite
for further review, not an authorization for execution. R4 remains a separate
NautilusTrader-inspired execution blueprint until positive executable evidence
survives this gate and receives explicit approval.
