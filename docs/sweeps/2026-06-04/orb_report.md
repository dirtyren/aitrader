# orb_equity sweep — 2026-06-04T00:15:02.854910+00:00

- Universe: `config/universe_russell_1000.csv` (445 symbols loaded, 3 missing from cache)
- Timeframe: `5Min`  Window: `2024-01-01 → 2026-08-31`
- IS/OOS split: 70/30 by bar count
- Grid: 48 combos over ['setups.orb_vwap.atr_mult_stop', 'setups.orb_vwap.orb_bars', 'setups.orb_vwap.target_R']

## Best global combo (selected)

**`atr_mult_stop=0.5, orb_bars=3, target_R=3.0`**

| metric | value |
|---|---|
| symbols_above_floor | 72 |
| symbols | 445 |
| total_oos_trades | 8147 |
| total_is_trades | 12023 |
| median_oos_sharpe | 0.1125 |
| mean_oos_sharpe | 0.0503 |
| total_oos_pnl | 16049.1210 |

### Chosen parameter values
```json
{
  "setups.orb_vwap.atr_mult_stop": 0.5,
  "setups.orb_vwap.orb_bars": 3,
  "setups.orb_vwap.target_R": 3.0
}
```

## Top-15 alternative combos

| combo | symbols≥floor | trades_oos | median_oos_sharpe | total_oos_pnl |
|---|---|---|---|---|
| atr_mult_stop=0.5, orb_bars=3, target_R=3.0 | 72 | 8147 | 0.113 | 16049 |
| atr_mult_stop=0.5, orb_bars=12, target_R=3.0 | 72 | 6593 | 0.063 | 4931 |
| atr_mult_stop=0.5, orb_bars=12, target_R=2.5 | 71 | 6600 | 0.040 | 1044 |
| atr_mult_stop=0.5, orb_bars=3, target_R=2.5 | 69 | 8159 | 0.106 | 9867 |
| atr_mult_stop=0.75, orb_bars=12, target_R=3.0 | 66 | 6551 | 0.042 | 7472 |
| atr_mult_stop=1.25, orb_bars=12, target_R=3.0 | 65 | 6427 | 0.005 | 10152 |
| atr_mult_stop=0.5, orb_bars=6, target_R=3.0 | 63 | 7537 | 0.093 | 6524 |
| atr_mult_stop=0.5, orb_bars=12, target_R=2.0 | 63 | 6623 | 0.023 | -3475 |
| atr_mult_stop=1.0, orb_bars=12, target_R=3.0 | 63 | 6487 | 0.014 | 9829 |
| atr_mult_stop=0.5, orb_bars=6, target_R=2.5 | 61 | 7546 | 0.081 | 1764 |
| atr_mult_stop=0.75, orb_bars=12, target_R=2.5 | 61 | 6557 | 0.032 | 2943 |
| atr_mult_stop=1.25, orb_bars=12, target_R=2.5 | 60 | 6431 | 0.000 | 6285 |
| atr_mult_stop=0.75, orb_bars=12, target_R=1.5 | 59 | 6592 | 0.000 | -7237 |
| atr_mult_stop=0.5, orb_bars=12, target_R=1.5 | 59 | 6635 | 0.000 | -9204 |
| atr_mult_stop=0.5, orb_bars=3, target_R=2.0 | 58 | 8181 | 0.077 | 3090 |

## Symbols surviving filter (48 kept; floor oos_sharpe≥0.3 AND oos_trades≥10)

| symbol | oos_sharpe | oos_trades | oos_pnl | oos_win_rate | is_sharpe | is_trades |
|---|---|---|---|---|---|---|
| STLD | 0.712 | 12 | 136 | 66.67% | 0.118 | 49 |
| MAR | 0.636 | 10 | 154 | 50.00% | 0.201 | 26 |
| LDOS | 0.577 | 15 | 233 | 60.00% | 0.185 | 6 |
| TECH | 0.565 | 17 | 75 | 70.59% | 0.137 | 76 |
| HES | 0.553 | 13 | 251 | 53.85% | 0.257 | 66 |
| EFX | 0.540 | 15 | 214 | 53.33% | 0.115 | 87 |
| AXP | 0.515 | 20 | 244 | 50.00% | -0.039 | 8 |
| UAL | 0.483 | 15 | 344 | 53.33% | -0.097 | 30 |
| SOFI | 0.480 | 23 | 348 | 43.48% | -0.168 | 14 |
| PNR | 0.474 | 23 | 356 | 69.57% | -0.484 | 10 |
| RVTY | 0.471 | 24 | 307 | 66.67% | 0.164 | 58 |
| KEYS | 0.467 | 13 | 56 | 53.85% | 0.265 | 73 |
| ZTS | 0.461 | 14 | 72 | 64.29% | 0.260 | 25 |
| ETN | 0.446 | 24 | 586 | 50.00% | 0.367 | 5 |
| NEM | 0.438 | 12 | 253 | 41.67% | -0.267 | 43 |
| MPC | 0.435 | 18 | 189 | 55.56% | -0.283 | 44 |
| EXPE | 0.434 | 34 | 424 | 55.88% | -0.240 | 45 |
| REGN | 0.423 | 16 | 11 | 56.25% | 0.064 | 16 |
| FAST | 0.417 | 14 | 275 | 50.00% | -0.063 | 18 |
| IRM | 0.398 | 15 | 90 | 53.33% | 0.000 | 1 |
| NSC | 0.395 | 10 | 126 | 50.00% | 0.134 | 43 |
| WYNN | 0.376 | 20 | 201 | 45.00% | -0.057 | 54 |
| J | 0.376 | 15 | 41 | 40.00% | 0.247 | 27 |
| CRM | 0.358 | 23 | 240 | 47.83% | 0.335 | 39 |
| TRV | 0.357 | 19 | 60 | 52.63% | 0.130 | 12 |
| ODFL | 0.351 | 37 | 201 | 54.05% | 0.239 | 5 |
| L | 0.349 | 16 | 23 | 50.00% | -0.166 | 3 |
| ALL | 0.347 | 15 | 62 | 60.00% | 0.167 | 2 |
| PHM | 0.346 | 14 | 183 | 64.29% | 0.234 | 54 |
| GRMN | 0.345 | 21 | 131 | 52.38% | 0.000 | 2 |
| FDX | 0.342 | 13 | 245 | 53.85% | -0.110 | 24 |
| EIX | 0.341 | 12 | 60 | 58.33% | 0.223 | 34 |
| GPC | 0.341 | 16 | 53 | 50.00% | 0.106 | 61 |
| MAA | 0.334 | 11 | -30 | 36.36% | 0.107 | 21 |
| VEEV | 0.332 | 12 | 184 | 66.67% | -0.086 | 10 |
| MTCH | 0.328 | 13 | 303 | 46.15% | 0.153 | 51 |
| DAL | 0.321 | 26 | 343 | 42.31% | 0.157 | 34 |
| HUM | 0.320 | 16 | 313 | 43.75% | 0.000 | 2 |
| WELL | 0.320 | 10 | 51 | 40.00% | 0.150 | 21 |
| TROW | 0.320 | 56 | 263 | 50.00% | 0.241 | 42 |
| HLT | 0.318 | 22 | 129 | 45.45% | 0.078 | 8 |
| ICE | 0.316 | 18 | 107 | 50.00% | 0.200 | 19 |
| TT | 0.313 | 35 | 333 | 45.71% | 0.831 | 4 |
| ESS | 0.308 | 29 | 43 | 37.93% | 0.341 | 18 |
| MHK | 0.307 | 12 | -12 | 33.33% | 0.280 | 49 |
| SUI | 0.304 | 45 | 372 | 57.78% | 0.172 | 46 |
| LHX | 0.302 | 12 | 71 | 50.00% | 0.141 | 75 |
| MLM | 0.301 | 21 | 222 | 57.14% | 0.219 | 27 |

## IS-vs-OOS distribution (selected combo)

| stat | IS sharpe | OOS sharpe | IS trades | OOS trades |
|---|---|---|---|---|
| mean | 0.131 | 0.050 | 27.0 | 18.3 |
| median | 0.138 | 0.113 | 23.0 | 16.0 |
| std | 0.491 | 0.451 | 20.6 | 12.5 |

## Proposed YAML diff (preview)

Apply to `config/settings_orb_equity.yaml`:

```yaml
setups:
  orb_vwap:
    atr_mult_stop: 0.5
  orb_vwap:
    orb_bars: 3
  orb_vwap:
    target_R: 3.0
asset_classes:
  equity:
    symbols:
      - STLD
      - MAR
      - LDOS
      - TECH
      - HES
      - EFX
      - AXP
      - UAL
      - SOFI
      - PNR
      - RVTY
      - KEYS
      - ZTS
      - ETN
      - NEM
      - MPC
      - EXPE
      - REGN
      - FAST
      - IRM
      - NSC
      - WYNN
      - J
      - CRM
      - TRV
      - ODFL
      - L
      - ALL
      - PHM
      - GRMN
      - FDX
      - EIX
      - GPC
      - MAA
      - VEEV
      - MTCH
      - DAL
      - HUM
      - WELL
      - TROW
      - HLT
      - ICE
      - TT
      - ESS
      - MHK
      - SUI
      - LHX
      - MLM
```


## Missing from cache (3 symbols, first 30)

AIG, AKAM, OXY