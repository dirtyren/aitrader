# vwap_bands_equity sweep — 2026-06-04T05:31:10.983058+00:00

- Universe: `config/universe_russell_1000.csv` (445 symbols loaded, 3 missing from cache)
- Timeframe: `5Min`  Window: `2024-01-01 → 2026-08-31`
- IS/OOS split: 70/30 by bar count
- Grid: 36 combos over ['setups.vwap_dev_bands.atr_mult_stop', 'setups.vwap_dev_bands.sigma', 'setups.vwap_dev_bands.target_R']

## Best global combo (selected)

**`atr_mult_stop=0.5, sigma=3.0, target_R=1.5`**

| metric | value |
|---|---|
| symbols_above_floor | 46 |
| symbols | 445 |
| total_oos_trades | 8798 |
| total_is_trades | 21631 |
| median_oos_sharpe | 0.0294 |
| mean_oos_sharpe | -0.0774 |
| total_oos_pnl | -11339.2501 |

### Chosen parameter values
```json
{
  "setups.vwap_dev_bands.atr_mult_stop": 0.5,
  "setups.vwap_dev_bands.sigma": 3.0,
  "setups.vwap_dev_bands.target_R": 1.5
}
```

## Top-15 alternative combos

| combo | symbols≥floor | trades_oos | median_oos_sharpe | total_oos_pnl |
|---|---|---|---|---|
| atr_mult_stop=0.5, sigma=3.0, target_R=1.5 | 46 | 8798 | 0.029 | -11339 |
| atr_mult_stop=0.5, sigma=3.0, target_R=2.0 | 46 | 8798 | 0.029 | -11339 |
| atr_mult_stop=0.5, sigma=3.0, target_R=2.5 | 46 | 8798 | 0.029 | -11339 |
| atr_mult_stop=0.75, sigma=3.0, target_R=1.5 | 35 | 8801 | -0.031 | -24044 |
| atr_mult_stop=0.75, sigma=3.0, target_R=2.0 | 35 | 8801 | -0.031 | -24044 |
| atr_mult_stop=0.75, sigma=3.0, target_R=2.5 | 35 | 8801 | -0.031 | -24044 |
| atr_mult_stop=1.0, sigma=3.0, target_R=1.5 | 35 | 8806 | -0.042 | -33716 |
| atr_mult_stop=1.0, sigma=3.0, target_R=2.0 | 35 | 8806 | -0.042 | -33716 |
| atr_mult_stop=1.0, sigma=3.0, target_R=2.5 | 35 | 8806 | -0.042 | -33716 |
| atr_mult_stop=0.5, sigma=2.5, target_R=1.5 | 7 | 29778 | 0.097 | 115 |
| atr_mult_stop=0.5, sigma=2.5, target_R=2.0 | 7 | 29778 | 0.097 | 115 |
| atr_mult_stop=0.5, sigma=2.5, target_R=2.5 | 7 | 29778 | 0.097 | 115 |
| atr_mult_stop=0.75, sigma=2.5, target_R=1.5 | 4 | 29797 | 0.046 | -38598 |
| atr_mult_stop=0.75, sigma=2.5, target_R=2.0 | 4 | 29797 | 0.046 | -38598 |
| atr_mult_stop=0.75, sigma=2.5, target_R=2.5 | 4 | 29797 | 0.046 | -38598 |

## Symbols surviving filter (36 kept; floor oos_sharpe≥0.3 AND oos_trades≥10)

| symbol | oos_sharpe | oos_trades | oos_pnl | oos_win_rate | is_sharpe | is_trades |
|---|---|---|---|---|---|---|
| MAS | 0.568 | 15 | 327 | 60.00% | 0.216 | 27 |
| KLAC | 0.548 | 18 | 503 | 55.56% | 0.212 | 62 |
| TTWO | 0.496 | 14 | 104 | 71.43% | 0.183 | 45 |
| MLM | 0.481 | 13 | 266 | 61.54% | 0.069 | 29 |
| DG | 0.464 | 14 | 256 | 42.86% | 0.269 | 42 |
| NTRS | 0.452 | 12 | 128 | 75.00% | -0.025 | 44 |
| APD | 0.429 | 13 | 96 | 46.15% | 0.133 | 57 |
| WAB | 0.428 | 12 | 156 | 58.33% | 0.156 | 51 |
| SBAC | 0.423 | 19 | 187 | 42.11% | 0.123 | 62 |
| BDX | 0.414 | 18 | 269 | 33.33% | 0.313 | 43 |
| PAYX | 0.411 | 16 | 131 | 50.00% | 0.135 | 49 |
| NWS | 0.410 | 18 | 212 | 55.56% | 0.136 | 28 |
| MAR | 0.410 | 19 | 174 | 57.89% | 0.237 | 44 |
| FITB | 0.407 | 22 | 242 | 31.82% | 0.056 | 44 |
| PG | 0.387 | 20 | 79 | 50.00% | 0.095 | 64 |
| WM | 0.380 | 15 | 162 | 46.67% | 0.112 | 51 |
| ADSK | 0.366 | 19 | 131 | 52.63% | 0.162 | 60 |
| WEC | 0.360 | 12 | 28 | 50.00% | -0.192 | 24 |
| LRCX | 0.358 | 16 | 832 | 43.75% | 0.043 | 48 |
| CHTR | 0.351 | 19 | 224 | 36.84% | 0.157 | 35 |
| WYNN | 0.349 | 16 | 105 | 43.75% | -0.103 | 20 |
| CDNS | 0.344 | 19 | 296 | 31.58% | 0.143 | 49 |
| NTAP | 0.342 | 10 | 114 | 40.00% | 0.186 | 37 |
| WRB | 0.339 | 10 | 120 | 50.00% | 0.163 | 29 |
| MMC | 0.331 | 20 | 191 | 45.00% | 0.043 | 34 |
| MCHP | 0.329 | 24 | 277 | 33.33% | 0.144 | 73 |
| WCN | 0.326 | 18 | 134 | 38.89% | 0.165 | 57 |
| ADBE | 0.323 | 13 | 125 | 38.46% | 0.166 | 45 |
| ADP | 0.321 | 19 | 184 | 52.63% | 0.181 | 34 |
| KMB | 0.320 | 22 | 208 | 36.36% | 0.173 | 32 |
| SYY | 0.315 | 15 | 68 | 46.67% | 0.159 | 50 |
| SYK | 0.309 | 14 | 120 | 35.71% | 0.081 | 52 |
| BA | 0.306 | 19 | 167 | 36.84% | -0.130 | 40 |
| IBKR | 0.306 | 18 | 209 | 44.44% | 0.182 | 56 |
| TDY | 0.301 | 12 | 63 | 50.00% | 0.298 | 29 |
| DASH | 0.301 | 22 | 2 | 27.27% | 0.277 | 59 |

## IS-vs-OOS distribution (selected combo)

| stat | IS sharpe | OOS sharpe | IS trades | OOS trades |
|---|---|---|---|---|
| mean | 0.056 | -0.077 | 48.6 | 19.8 |
| median | 0.109 | 0.029 | 48.0 | 18.0 |
| std | 0.185 | 0.434 | 17.8 | 9.6 |

## Proposed YAML diff (preview)

Apply to `config/settings_vwap_bands_equity.yaml`:

```yaml
setups:
  vwap_dev_bands:
    atr_mult_stop: 0.5
  vwap_dev_bands:
    sigma: 3.0
  vwap_dev_bands:
    target_R: 1.5
asset_classes:
  equity:
    symbols:
      - MAS
      - KLAC
      - TTWO
      - MLM
      - DG
      - NTRS
      - APD
      - WAB
      - SBAC
      - BDX
      - PAYX
      - NWS
      - MAR
      - FITB
      - PG
      - WM
      - ADSK
      - WEC
      - LRCX
      - CHTR
      - WYNN
      - CDNS
      - NTAP
      - WRB
      - MMC
      - MCHP
      - WCN
      - ADBE
      - ADP
      - KMB
      - SYY
      - SYK
      - BA
      - IBKR
      - TDY
      - DASH
```


## Missing from cache (3 symbols, first 30)

AIG, AKAM, OXY