# Russell 1000 Equity Strategy Sweep — Summary

> **Run window:** 2024-01-01 → 2026-08-31 (32 months)
> **Universe:** `config/universe_russell_1000.csv` (445 symbols cached, 3 timed out)
> **Methodology:** per-strategy grid sweep × per-symbol replay; IS/OOS 70/30; pick global-best combo by `symbols_above_floor` (OOS Sharpe ≥ 0.3 AND OOS trades ≥ 10), break ties on median OOS Sharpe and total OOS PnL.
> **Detail per strategy:** `runtime/wfo/<strategy>_pilot/report.md`.

## Headline ranking

Sorted by `symbols_above_floor` then `median_oos_sharpe`. **Bigger is better on Sharpe; PnL is the universe-aggregate after slippage but before commissions.**

| strategy   | timeframe | best params (combo)                                          | survivors | median_oos_sharpe | total_oos_pnl | total_oos_trades |
|------------|-----------|--------------------------------------------------------------|----------:|------------------:|--------------:|------------------:|
| **orb**    | 5Min      | `atr_mult_stop=0.5, orb_bars=3, target_R=3.0`                | **72**    |          **0.11** |     **+$16k** |             8,147 |
| vwap_bands | 5Min      | `atr_mult_stop=0.5, sigma=3.0, target_R=1.5`                 |        46 |              0.03 |        −$11k  |             8,798 |
| rsi        | 1Hour     | `period=5, threshold=30, stop_loss_pct=2.5`                  |        19 |              0.05 |       +$211k  |            19,324 |
| ib         | 1Hour     | `atr_mult_stop=0.75, ib_bars=5, target_R=1.0`                |         2 |             −0.96 |       −$401k  |             3,833 |
| vwap_wave  | 5Min      | `arm_window_bars=4, atr_mult_stop=0.75, target_R=2.5` (price_discovery only) |         0 |             −0.02 |       −$224k  |            76,521 |

## What this means, honestly

- **`orb_equity` is the clearest winner on the broad universe.** 72 symbols clear the OOS Sharpe ≥ 0.3 floor, the highest median OOS Sharpe (0.11), and a positive aggregate PnL. The chosen combo (tight 0.5 ATR stop, 3-bar opening range, 3R target) deviates noticeably from the production default (1.0 ATR stop, 6-bar range, 3.0R target).
- **`rsi_equity` has positive aggregate PnL** (+$211k), but only 19 symbols clear the floor — and the median OOS Sharpe is barely above noise (0.05). Useful as a small, curated piece of the book; not a broad-universe edge.
- **`vwap_bands_equity` looks like a curate-and-keep candidate.** 46 surviving symbols at the chosen combo, but aggregate PnL is slightly negative — meaning the loser tail in the universe drags the total down. The surviving subset specifically may be profitable.
- **`ib_equity` does not work on this universe.** Aggregate −$401k across 447 symbols at every grid point. Median OOS Sharpe of −0.96 is squarely in "the strategy loses systematically" territory. The grid also revealed `atr_mult_stop` and `target_R` are vestigial — exits are dominated by `time_stop` rather than stop/target hits, so those knobs don't move the needle.
- **`vwap_wave_equity` (the production multi-setup strategy) lost on the broad universe.** 0 symbols cleared the floor at the swept `price_discovery` settings, with the other three setups left at production defaults. Caveat: the sweep only varied `price_discovery` parameters; the other 3 setups (`fade_extreme`, `return_to_value`, `vwap_bounce`) might dominate total signal density and be the actual profit/loss drivers. **A vwap_wave-specific sweep that also varies the other setups, or selectively disables them, is required before any conclusion**.

## Curated universes (proposed)

Each report contains the full surviving-symbols table and a YAML diff. Highlights:

- **orb (top 15 of 72 by OOS Sharpe):** STLD, MAR, LDOS, TECH, HES, EFX, AXP, UAL, SOFI, PNR, RVTY, …
- **rsi (all 19):** CSCO, STLD, PWR, WBA, AMAT, TGT, WAB, NUE, HPE, SQ, GLW, EQR, CTVA, GEV, JBHT, CSX, KEYS, HAL, CVX
- **vwap_bands (top 15 of 46):** in `runtime/wfo/vwap_bands_pilot/report.md`
- **ib:** only MKC survived, with OOS Sharpe 0.32 — but on just 10 OOS trades and −$611 PnL. Not actionable.
- **vwap_wave:** none.

**Important: zero overlap between any current production universe and the surviving list per strategy.** Today's RSI symbols (SPY, QQQ, AAPL, MSFT, NVDA, JPM, NFLX, COIN, PLTR, UBER) include zero of the 19 RSI survivors (CSCO, STLD, …).

## Caveats / what this sweep does NOT prove

1. **Single split, not walk-forward.** One IS/OOS split per symbol. No rolling re-fit. A walk-forward run could surface combos that adapt to regime shifts but cost ~10× more compute.
2. **Per-trade Sharpe, not annualized.** Sharpe values here are R-multiple Sharpe per trade; comparable across combos within a strategy but not directly to industry benchmarks.
3. **Slippage 2 bps, no commissions.** Realistic deductions would shave OOS PnL further. RSI's +$211k → maybe +$150k after commissions.
4. **Grid coverage is modest.** RSI: 36 combos, IB: 48, ORB: 48, vwap_bands: 36, vwap_wave: 27 (price_discovery only). Wider grids (add `max_hold_bars`, `consecutive_loss_limit`, etc.) might find better local optima.
5. **`vwap_wave` only varied one of its four setups.** Conclusion is conditional on the other three (`fade_extreme`, `return_to_value`, `vwap_bounce`) at their production defaults.
6. **3 symbols missing from cache** (AIG, AKAM, OXY for 5Min; O for 1Hour) due to Alpaca timeouts. Re-running `scripts/cache_bars_universe.py` would fill them in.
7. **The strategy code itself was buggy in backtest mode** — three correctness fixes shipped in this PR (PositionManager fill-confirmation gate, missing positions_snapshot kwarg in `record_exits_to_ledger`, daily ledger reset). Without those fixes every prior backtest produced ~0 trades regardless of params.

## Recommended next moves

1. **Land orb_equity universe + params**, but stage carefully — switch to the 72-symbol curated universe with the new params (`atr_mult_stop=0.5, orb_bars=3, target_R=3.0`) on **paper** first; observe live trade counts and PnL before promoting.
2. **Don't switch RSI yet.** Aggregate edge is too thin (median Sharpe 0.05). Either re-sweep with a wider grid (different timeframes, longer history), or leave RSI on its current curated universe and let `orb` carry the equity exposure.
3. **Audit vwap_wave** — re-run with all four setups in the grid (or selectively disabled) to identify which setup carries the (negative) aggregate. Possibly the only profitable setup is overshadowed by losses from another.
4. **Fix or retire ib_equity.** A strategy that loses at every grid point is either fundamentally mispriced for this regime or has a bug. The time-stop dominance suggests the entry signal isn't actionable on the timescale `max_hold_bars=24` allows. Worth a focused investigation, not another sweep.
5. **Engine fixes** in this PR (`backtest/intraday_replay.py`) make the existing tests in `tests/test_backtest_sanity.py` more accurate but do not change live behavior.
