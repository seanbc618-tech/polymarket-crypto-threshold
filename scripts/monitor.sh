#!/bin/bash
set -euo pipefail

# Read-only discovery monitor. It never supplies a synthetic market probability
# and never calls an order endpoint.

INTERVAL=${1:-5}
LOG_DIR="runs/monitor"
LOG_FILE="${LOG_DIR}/monitor-$(date +%Y%m%d).log"
mkdir -p "$LOG_DIR"

while true; do
  {
    echo "SCAN: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    uv run crypto-threshold discover --asset BTC
    uv run crypto-threshold discover --asset ETH
    uv run crypto-threshold markets --limit 20
  } | tee -a "$LOG_FILE"
  sleep "$((INTERVAL * 60))"
done
