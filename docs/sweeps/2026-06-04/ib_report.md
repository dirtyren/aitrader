# ib_equity sweep — 2026-06-03T16:30:32.615746+00:00

- Universe: `config/universe_russell_1000.csv` (447 symbols loaded, 1 missing from cache)
- Timeframe: `1Hour`  Window: `2024-01-01 → 2026-08-31`
- IS/OOS split: 70/30 by bar count
- Grid: 48 combos over ['setups.initial_balance.atr_mult_stop', 'setups.initial_balance.ib_bars', 'setups.initial_balance.target_R']

## Best global combo (selected)

**`atr_mult_stop=0.75, ib_bars=5, target_R=1.0`**

| metric | value |
|---|---|
| symbols_above_floor | 2 |
| symbols | 447 |
| total_oos_trades | 3833 |
| total_is_trades | 5476 |
| median_oos_sharpe | -0.9637 |
| mean_oos_sharpe | -1.5558 |
| total_oos_pnl | -400718.5258 |

### Chosen parameter values
```json
{
  "setups.initial_balance.atr_mult_stop": 0.75,
  "setups.initial_balance.ib_bars": 5,
  "setups.initial_balance.target_R": 1.0
}
```

## Top-15 alternative combos

| combo | symbols≥floor | trades_oos | median_oos_sharpe | total_oos_pnl |
|---|---|---|---|---|
| atr_mult_stop=0.75, ib_bars=5, target_R=1.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=0.75, ib_bars=5, target_R=1.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=0.75, ib_bars=5, target_R=2.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=0.75, ib_bars=5, target_R=2.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.0, ib_bars=5, target_R=1.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.0, ib_bars=5, target_R=1.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.0, ib_bars=5, target_R=2.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.0, ib_bars=5, target_R=2.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.25, ib_bars=5, target_R=1.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.25, ib_bars=5, target_R=1.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.25, ib_bars=5, target_R=2.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.25, ib_bars=5, target_R=2.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.5, ib_bars=5, target_R=1.0 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.5, ib_bars=5, target_R=1.5 | 2 | 3833 | -0.964 | -400719 |
| atr_mult_stop=1.5, ib_bars=5, target_R=2.0 | 2 | 3833 | -0.964 | -400719 |

## Symbols surviving filter (1 kept; floor oos_sharpe≥0.3 AND oos_trades≥10)

| symbol | oos_sharpe | oos_trades | oos_pnl | oos_win_rate | is_sharpe | is_trades |
|---|---|---|---|---|---|---|
| MKC | 0.317 | 10 | -611 | 30.00% | -1.294 | 20 |

## IS-vs-OOS distribution (selected combo)

| stat | IS sharpe | OOS sharpe | IS trades | OOS trades |
|---|---|---|---|---|
| mean | -1.731 | -1.556 | 12.3 | 8.6 |
| median | -0.800 | -0.964 | 10.0 | 7.0 |
| std | 9.972 | 2.282 | 9.4 | 6.4 |

## Proposed YAML diff (preview)

Apply to `config/settings_ib_equity.yaml`:

```yaml
setups:
  initial_balance:
    atr_mult_stop: 0.75
  initial_balance:
    ib_bars: 5
  initial_balance:
    target_R: 1.0
asset_classes:
  equity:
    symbols:
      - MKC
```


## Missing from cache (1 symbols, first 30)

O