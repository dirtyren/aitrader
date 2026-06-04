# rsi_equity sweep — 2026-06-03T15:55:48.178230+00:00

- Universe: `config/universe_russell_1000.csv` (447 symbols loaded, 1 missing from cache)
- Timeframe: `1Hour`  Window: `2024-01-01 → 2026-08-31`
- IS/OOS split: 70/30 by bar count
- Grid: 36 combos over ['setups.rsi_reversion.period', 'setups.rsi_reversion.stop_loss_pct', 'setups.rsi_reversion.threshold']

## Best global combo (selected)

**`period=5, stop_loss_pct=2.5, threshold=30`**

| metric | value |
|---|---|
| symbols_above_floor | 19 |
| symbols | 447 |
| total_oos_trades | 19324 |
| total_is_trades | 40667 |
| median_oos_sharpe | 0.0487 |
| mean_oos_sharpe | 0.0441 |
| total_oos_pnl | 211371.9207 |

### Chosen parameter values
```json
{
  "setups.rsi_reversion.period": 5,
  "setups.rsi_reversion.stop_loss_pct": 2.5,
  "setups.rsi_reversion.threshold": 30
}
```

## Top-15 alternative combos

| combo | symbols≥floor | trades_oos | median_oos_sharpe | total_oos_pnl |
|---|---|---|---|---|
| period=5, stop_loss_pct=2.5, threshold=30 | 19 | 19324 | 0.049 | 211372 |
| period=5, stop_loss_pct=1.5, threshold=30 | 16 | 24424 | 0.068 | 195813 |
| period=5, stop_loss_pct=2.0, threshold=35 | 16 | 24025 | 0.060 | 253005 |
| period=5, stop_loss_pct=2.0, threshold=30 | 15 | 21451 | 0.046 | 205358 |
| period=5, stop_loss_pct=2.5, threshold=35 | 14 | 21438 | 0.051 | 264340 |
| period=5, stop_loss_pct=2.0, threshold=40 | 12 | 26475 | 0.048 | 254448 |
| period=5, stop_loss_pct=2.5, threshold=45 | 12 | 24928 | 0.045 | 280430 |
| period=5, stop_loss_pct=2.0, threshold=45 | 10 | 28484 | 0.053 | 264057 |
| period=5, stop_loss_pct=1.5, threshold=35 | 9 | 27732 | 0.073 | 240233 |
| period=5, stop_loss_pct=2.5, threshold=40 | 7 | 23317 | 0.050 | 261294 |
| period=5, stop_loss_pct=1.5, threshold=40 | 6 | 30988 | 0.068 | 246633 |
| period=5, stop_loss_pct=1.5, threshold=45 | 5 | 33771 | 0.073 | 265075 |

## Symbols surviving filter (19 kept; floor oos_sharpe≥0.3 AND oos_trades≥10)

| symbol | oos_sharpe | oos_trades | oos_pnl | oos_win_rate | is_sharpe | is_trades |
|---|---|---|---|---|---|---|
| CSCO | 0.559 | 33 | 4166 | 63.64% | 0.217 | 83 |
| STLD | 0.496 | 38 | 5243 | 57.89% | -0.014 | 110 |
| PWR | 0.411 | 39 | 4499 | 61.54% | 0.059 | 86 |
| WBA | 0.382 | 25 | 1076 | 72.00% | -0.093 | 87 |
| AMAT | 0.349 | 51 | 4797 | 52.94% | 0.014 | 110 |
| TGT | 0.347 | 39 | 3491 | 51.28% | 0.142 | 106 |
| WAB | 0.342 | 40 | 3347 | 57.50% | 0.147 | 75 |
| NUE | 0.341 | 36 | 3615 | 55.56% | -0.015 | 109 |
| HPE | 0.339 | 52 | 4673 | 46.15% | 0.131 | 101 |
| SQ | 0.337 | 21 | 2062 | 52.38% | -0.175 | 58 |
| GLW | 0.334 | 57 | 5936 | 49.12% | 0.255 | 90 |
| EQR | 0.334 | 38 | 2464 | 60.53% | -0.022 | 81 |
| CTVA | 0.327 | 38 | 3110 | 55.26% | 0.213 | 79 |
| GEV | 0.326 | 45 | 3644 | 51.11% | 0.145 | 95 |
| JBHT | 0.316 | 34 | 2977 | 52.94% | 0.084 | 100 |
| CSX | 0.316 | 34 | 2458 | 61.76% | 0.105 | 80 |
| KEYS | 0.313 | 43 | 4069 | 55.81% | 0.142 | 92 |
| HAL | 0.312 | 46 | 4369 | 54.35% | -0.066 | 102 |
| CVX | 0.311 | 40 | 3501 | 55.00% | 0.150 | 89 |

## IS-vs-OOS distribution (selected combo)

| stat | IS sharpe | OOS sharpe | IS trades | OOS trades |
|---|---|---|---|---|
| mean | 0.077 | 0.044 | 91.0 | 43.2 |
| median | 0.077 | 0.049 | 87.0 | 41.0 |
| std | 0.098 | 0.152 | 16.8 | 9.0 |

## Proposed YAML diff (preview)

Apply to `config/settings_rsi_equity.yaml`:

```yaml
setups:
  rsi_reversion:
    period: 5
  rsi_reversion:
    stop_loss_pct: 2.5
  rsi_reversion:
    threshold: 30
asset_classes:
  equity:
    symbols:
      - CSCO
      - STLD
      - PWR
      - WBA
      - AMAT
      - TGT
      - WAB
      - NUE
      - HPE
      - SQ
      - GLW
      - EQR
      - CTVA
      - GEV
      - JBHT
      - CSX
      - KEYS
      - HAL
      - CVX
```


## Missing from cache (1 symbols, first 30)

O