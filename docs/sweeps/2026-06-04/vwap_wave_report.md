# vwap_wave_equity sweep — 2026-06-04T12:57:28.239203+00:00

- Universe: `config/universe_russell_1000.csv` (445 symbols loaded, 3 missing from cache)
- Timeframe: `5Min`  Window: `2024-01-01 → 2026-08-31`
- IS/OOS split: 70/30 by bar count
- Grid: 27 combos over ['setups.price_discovery.arm_window_bars', 'setups.price_discovery.atr_mult_stop', 'setups.price_discovery.target_R']

## Best global combo (selected)

**`arm_window_bars=4, atr_mult_stop=0.75, target_R=2.5`**

| metric | value |
|---|---|
| symbols_above_floor | 0 |
| symbols | 445 |
| total_oos_trades | 76521 |
| total_is_trades | 151045 |
| median_oos_sharpe | -0.0215 |
| mean_oos_sharpe | -0.0316 |
| total_oos_pnl | -223532.2546 |

### Chosen parameter values
```json
{
  "setups.price_discovery.arm_window_bars": 4,
  "setups.price_discovery.atr_mult_stop": 0.75,
  "setups.price_discovery.target_R": 2.5
}
```

## Top-15 alternative combos

| combo | symbols≥floor | trades_oos | median_oos_sharpe | total_oos_pnl |
|---|---|---|---|---|
| arm_window_bars=4, atr_mult_stop=0.75, target_R=2.5 | 0 | 76521 | -0.021 | -223532 |
| arm_window_bars=4, atr_mult_stop=1.0, target_R=2.5 | 0 | 76521 | -0.021 | -223532 |
| arm_window_bars=4, atr_mult_stop=1.25, target_R=2.5 | 0 | 76521 | -0.021 | -223532 |
| arm_window_bars=6, atr_mult_stop=0.75, target_R=2.5 | 0 | 77038 | -0.023 | -222375 |
| arm_window_bars=6, atr_mult_stop=1.0, target_R=2.5 | 0 | 77038 | -0.023 | -222375 |
| arm_window_bars=6, atr_mult_stop=1.25, target_R=2.5 | 0 | 77038 | -0.023 | -222375 |
| arm_window_bars=8, atr_mult_stop=0.75, target_R=2.5 | 0 | 77378 | -0.023 | -222404 |
| arm_window_bars=8, atr_mult_stop=1.0, target_R=2.5 | 0 | 77378 | -0.023 | -222404 |
| arm_window_bars=8, atr_mult_stop=1.25, target_R=2.5 | 0 | 77378 | -0.023 | -222404 |
| arm_window_bars=4, atr_mult_stop=0.75, target_R=2.0 | 0 | 76537 | -0.025 | -227543 |
| arm_window_bars=4, atr_mult_stop=1.0, target_R=2.0 | 0 | 76537 | -0.025 | -227543 |
| arm_window_bars=4, atr_mult_stop=1.25, target_R=2.0 | 0 | 76537 | -0.025 | -227543 |
| arm_window_bars=8, atr_mult_stop=0.75, target_R=2.0 | 0 | 77402 | -0.027 | -226433 |
| arm_window_bars=8, atr_mult_stop=1.0, target_R=2.0 | 0 | 77402 | -0.027 | -226433 |
| arm_window_bars=8, atr_mult_stop=1.25, target_R=2.0 | 0 | 77402 | -0.027 | -226433 |

## Symbols surviving filter (0 kept; floor oos_sharpe≥0.3 AND oos_trades≥10)

**None.** Strategy did not surface a clean universe at this Sharpe floor.


## IS-vs-OOS distribution (selected combo)

| stat | IS sharpe | OOS sharpe | IS trades | OOS trades |
|---|---|---|---|---|
| mean | -0.002 | -0.032 | 339.4 | 172.0 |
| median | 0.009 | -0.021 | 354.0 | 173.0 |
| std | 0.074 | 0.113 | 83.2 | 37.7 |

## Proposed YAML diff (preview)

Apply to `config/settings_vwap_wave_equity.yaml`:

```yaml
setups:
  price_discovery:
    arm_window_bars: 4
  price_discovery:
    atr_mult_stop: 0.75
  price_discovery:
    target_R: 2.5
```


## Missing from cache (3 symbols, first 30)

AIG, AKAM, OXY