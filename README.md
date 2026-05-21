# vwap_wave

## Overview

`vwap_wave` is an autonomous intraday trading system implementing the **VWAP Wave Protocol** across equities, ETFs, and crypto via Alpaca Markets. The engine treats VWAP as a dynamic Point of Control, classifies each session as Range / Discovery / Trend, and runs four setup state machines:

1. **Price Discovery Continuation** — band breakout + acceptance + retest entry.
2. **Fade Value Area Extremes** — scale-in fades on balance days.
3. **Return to Value** — failed discovery move re-entering the value area.
4. **VWAP Bounce** — trend-day reclaim after a sub-VWAP liquidity trap.

Live execution and backtesting share the same `SessionContext`, setup, filter, and risk classes; only the bar source and order sink differ.

## Architecture

```
vwap_wave/
├── config/settings.yaml        # All tunable parameters
├── core/
│   ├── bar.py                  # Bar dataclass (OHLCV, timezone-aware)
│   ├── vwap.py                 # Incremental VWAP + ±1σ bands
│   ├── acceptance.py           # N-close + ATR distance detector
│   ├── atr.py                  # Wilder ATR
│   ├── asset_class.py          # AssetClassConfig + session boundary
│   ├── session.py              # SessionContext per symbol
│   └── position_manager.py     # Stop / target / breakeven / time-stop
├── strategies/
│   ├── base_setup.py           # BaseSetup + SetupSignal
│   ├── regime_detector.py      # Range / Trend / Discovery classifier
│   ├── setup_price_discovery.py
│   ├── setup_fade_extreme.py
│   ├── setup_return_to_value.py
│   └── setup_vwap_bounce.py
├── risk/
│   ├── circuit_breakers.py     # Tiered P&L breakers + lock-file
│   ├── filters.py              # 8 entry filters + pipeline
│   ├── sizing.py               # ATR-based position sizing
│   └── manager.py              # Façade: pipeline → sizing → RiskDecision
├── state/
│   ├── position_book.py        # Open positions ledger
│   ├── daily_ledger.py         # Per-day P&L + consecutive losses
│   └── dashboard_state.py      # Atomic JSON for dashboard
├── broker/
│   ├── alpaca_client.py        # REST API (orders, account, bars, brackets, replace)
│   ├── alpaca_data.py          # Bars wrapper + parquet cache
│   ├── order_executor.py       # SetupSignal + RiskDecision → broker order
│   └── symbol.py               # Equity vs crypto symbol helpers
├── scheduler/
│   ├── bar_clock.py            # next_boundary, sleep_until
│   └── loop.py                 # VWAPWaveEngine.tick (bar-close cycle)
├── backtest/
│   ├── intraday_replay.py      # Shared-engine historical replay
│   ├── fill_engine.py          # SimulatedFillEngine
│   └── performance.py          # compute_metrics (R-multiple model)
├── ui/
│   ├── dashboard.py            # Streamlit panels
│   └── logging_setup.py
├── main.py                     # Bar-close scheduler boot
├── requirements.txt
└── tests/                      # Unit + integration tests (~1 s full suite)
```

## Setup

### Requirements

- Python 3.11+
- `pip install -r requirements.txt`

### Environment Variables

| Variable           | Required | Default                              |
|--------------------|----------|--------------------------------------|
| `ALPACA_API_KEY`   | Yes      | —                                    |
| `ALPACA_SECRET_KEY`| Yes      | —                                    |
| `ALPACA_BASE_URL`  | No       | `https://paper-api.alpaca.markets`   |
| `TRADING_ENV`      | No       | `production` (`test` to bypass lock) |
| `LOCK_FILE_PATH`   | No       | `lock.file`                          |

Create a `.env` in the project root:

```
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
```

```bash
mkdir -p logs runtime/bars_cache
```

## Running

### Live (paper) trading

```bash
python main.py
```

The engine wakes at each 5-minute bar boundary, fetches fresh bars for every configured symbol, runs the four setup state machines, evaluates risk filters, sizes orders, and submits them via Alpaca. Live trading is gated behind `system.trading_env: paper` — flipping to `live` is intentional and ungated.

### Dashboard

```bash
streamlit run ui/dashboard.py
```

The dashboard auto-refreshes every 5 s and reads `runtime/trading_state.json`.

### Backtest

```python
from backtest.intraday_replay import IntradayReplay
from core.asset_class import AssetClassConfig
import yaml

cfg = yaml.safe_load(open("config/settings.yaml"))

# Build asset class configs and (symbol, asset_class) list as in main.py;
# helper functions are shared — see main.py:build_asset_class_configs.

result = IntradayReplay(symbols=..., asset_class_configs=...,
                        bars=..., initial_equity=cfg["backtest"]["initial_equity"],
                        config=cfg).run()
print(result.metrics)
```

A runnable `scripts/run_backtest.py` can be added as a follow-up.

### Walk-Forward Optimization

```bash
python -m scripts.run_wfo --config config/wfo.yaml
```

Tunes setup parameters per `(symbol, timeframe)` over rolling IS/OOS windows
across the broker's tradable universe. Output lands under
`runtime/wfo/<run_id>/`:

- `results.parquet` — every `(symbol, timeframe, walk, combo)` IS/OOS row.
- `live_overrides.yaml` — per-symbol best `(timeframe, setup, params)` for
  symbols whose aggregate **WFE ≥ 0.5** AND total OOS P&L > 0 (Pardo gate).
- `summary.md` — ranked human-readable table.

`runtime/wfo/<run_id>/live_overrides.yaml` is the immutable per-run candidate.
The dashboard's WFO tab approves per-symbol candidates into
`runtime/wfo/active/live_overrides.yaml`, which `main.py` reads at boot and
layers on top of `config/settings.yaml`. Approvals take effect on the next
trader restart; an audit log is kept at `runtime/wfo/active/audit.jsonl`.

Tunables: `config/wfo.yaml` (universe scan, IS/OOS lengths in days/months,
parameter grids per setup, fitness floor, gate thresholds).

## Circuit Breakers / Lock-File Recovery

Three escalating tiers (tunable in `config/settings.yaml`):

- **L1** (−1.5 % intraday): position sizes halved.
- **L2** (−2.5 % intraday): new entries blocked for 24 h.
- **L3** (−5 % peak-to-valley): `lock.file` written, `sys.exit(1)`.

Recovery: review logs, perform post-mortem, then `rm lock.file` and restart. The lock-file guard runs before any heavy import, so an unresolved emergency halt cannot be bypassed by a subsequent code path.

## Testing

```bash
pytest tests/ -v
```

No broker connectivity required for the main suite; mocks are used throughout. Smoke-import the boot path with:

```bash
TRADING_ENV=test ALPACA_API_KEY=x ALPACA_SECRET_KEY=x python -c "import main; print('ok')"
```
