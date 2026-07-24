#!/bin/bash
set -euo pipefail

# Read-only discovery helper. Strategy ranking belongs to MarketWorkflowService,
# which requires a real Gamma market ID and executable CLOB asks.

uv run crypto-threshold discover --asset BTC
uv run crypto-threshold discover --asset ETH
uv run crypto-threshold markets --limit 20
