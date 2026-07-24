---
name: market-analyzer
description: Analyze crypto threshold markets and identify trading opportunities
invocation: both
---

# Market Analyzer Skill

## Purpose
Automate crypto market analysis workflow for BTC and ETH threshold markets.

## Quick Commands

### Check Current Prices
```bash
# BTC price
uv run crypto-threshold prices --asset BTC

# ETH price
uv run crypto-threshold prices --asset ETH
```

### Analyze Market
```bash
# Standard analysis
uv run crypto-threshold analyze 'Will Bitcoin be above $100,000 on June 30?' --market-prob 0.02

# With custom market probability
uv run crypto-threshold analyze 'Will ETH be below $1,500 by July 15?' --market-prob 0.10
```

### System Status
```bash
# Health check
uv run crypto-threshold doctor

# Run tests
uv run pytest -q

# Linting
uv run ruff check src/ tests/
```

## Analysis Workflow

1. **Fetch Prices**
   - Get current BTC/ETH spot prices
   - Verify cross-check passes (< 0.5% diff)

2. **Parse Market Question**
   - Extract asset (BTC/ETH)
   - Extract operator (above/below)
   - Extract threshold ($X)
   - Extract deadline (date)

3. **Calculate Probability**
   - Use Black-Scholes model
   - Apply volatility estimate
   - Generate confidence interval

4. **Compute Edge**
   - Edge = Model Probability - Market Probability
   - Positive edge = value opportunity
   - Negative edge = no value

5. **Generate Report**
   - Asset, operator, threshold, deadline
   - Spot price, probability estimate
   - Edge calculation
   - Recommendations

## Edge Interpretation

| Edge | Meaning | Action |
|------|---------|--------|
| > 15% | Strong opportunity | Consider immediate action |
| 10-15% | Good opportunity | Monitor closely |
| 5-10% | Moderate opportunity | Track periodically |
| 0-5% | Weak opportunity | Low priority |
| < 0% | No value | Market overpriced |

## Top Opportunities Template

When analyzing markets, format output as:

```
🎯 TOP OPPORTUNITIES

1. [Market Description]
   Market Prob: X% | Model Est: Y% | Edge: +Z%
   💡 [Insight]

2. [Market Description]
   Market Prob: X% | Model Est: Y% | Edge: +Z%
   💡 [Insight]
```

## Safety Reminders

- ⚠️ TRADING_DISABLED defaults to true
- ⚠️ All estimates use default 80% volatility
- ⚠️ Confidence levels are "low"
- ⚠️ Re-analyze before any action
- ⚠️ Monitor price movements

## Example Usage

```bash
# Quick scan
./scripts/quick-scan.sh

# Analyze specific market
uv run crypto-threshold analyze 'Will Bitcoin be below $62,000 on June 30?' --market-prob 0.15

# Check edge
# Output: Edge: +26% (model sees value)
```

## Integration

This skill integrates with:
- Quick scan scripts (`scripts/quick-scan.sh`)
- Monitor scripts (`scripts/monitor.sh`)
- Market analysis docs (`docs/runbooks/live-market-analysis.md`)
- Monitoring plan (`docs/plans/real-time-monitoring-plan.md`)
