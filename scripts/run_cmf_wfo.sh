#!/bin/bash
# WFO runner for CMF strategy
set -e
cd /home/hermes/aitrader

# Source the .env file for Alpaca credentials
set -a
source config/.env
set +a

# Map to the vars AlpacaClient expects when no asset_class is passed
export ALPACA_API_KEY="$ALPACA_EQUITY_API_KEY"
export ALPACA_SECRET_KEY="$ALPACA_EQUITY_SECRET_KEY"
export ALPACA_BASE_URL="$ALPACA_EQUITY_BASE_URL"

# Run WFO
PYTHONPATH=. python3 scripts/run_wfo.py \
  --config config/wfo_cmf.yaml \
  --settings config/settings_cmf_equity.yaml \
  --verbose 2>&1
