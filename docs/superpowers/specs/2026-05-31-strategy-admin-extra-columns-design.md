# Strategy admin table — Sharpe, Max DD, Avg R columns

**Status:** Draft
**Date:** 2026-05-31
**Branch:** `feat/strategy-enable-disable`

## Goal

Add three risk-adjusted columns — Sharpe, Max Drawdown, Avg R — to the existing
per-strategy kill-switch table on the Strategies tab. Make the table's metric
columns honor the page's period selector.

## Context

Commit `59786c0` added a per-strategy kill-switch admin panel
(`ui/tabs/strategies_tab.py:_render_admin_panel`) that lists Name · State ·
Open · Today P&L · Total P&L · Win rate · Action. The data is sourced from
`MySQLStore.get_strategies_admin_view()` which aggregates open count, today and
all-time P&L, and win rate in a single pass.

Operators have asked for the same risk-adjusted metrics that already appear on
the per-strategy detail KPI row: Sharpe, Max Drawdown, Avg R (expectancy in R
units). Those are already computed by `ui/data/stats.py:compute_kpis(df)`.

## Approach

Merge `compute_kpis` results into each admin row in the dashboard layer, no
schema or SQL changes.

`ui/data/strategy_admin.py:get_admin_view` gains `(start, end)` parameters.
For each strategy in the admin view, it fetches that strategy's closed trades
for the period via `trades_repo.get_closed_trades(name, start, end)`, runs
`stats.compute_kpis(df)` on the result, and merges the kpis into the row.

Sharpe in SQL is awkward (per-day grouping, stddev, annualization). Reusing
`compute_kpis` keeps a single source of truth for these metrics across the
detail KPI row, the strategy cards, and the admin table.

## Scope changes vs. the existing table

The page's period selector currently affects only the strategy cards below the
admin panel; the admin table is all-time. After this change:

- **Today P&L** — unchanged, today only (operator reads "today" literally).
  Comes from the existing SQL aggregator (`get_strategies_admin_view`).
- **P&L** (renamed from "Total P&L"), **Win rate**, **Sharpe**, **Max DD**,
  **Avg R** — period-scoped. Computed from the same closed-trades dataframe
  the cards use.
- **Open count** — unchanged, current state, not period-scoped.
- **State, Action** — unchanged.

The "Total P&L" → "P&L" rename signals the column is no longer all-time.

## Final columns

Name · State · Open · Today P&L · P&L · Win rate · Sharpe · Max DD · Avg R · Action

Column widths in `st.columns(...)`: `[2, 1, 1, 1, 1, 1, 1, 1, 1, 2]`.

## Formatting

| Column   | Format            | None / empty |
| -------- | ----------------- | ------------ |
| P&L      | `{:+.2f}`         | n/a (0.00)   |
| Today PL | `{:+.2f}`         | n/a (0.00)   |
| Win rate | `{:.1f}%`         | em-dash      |
| Sharpe   | `{:.2f}`          | em-dash      |
| Max DD   | `{:+.0f}`         | em-dash      |
| Avg R    | `{:+.2f}R`        | em-dash      |

`compute_kpis` returns `None` for Sharpe when fewer than two trading days are
present and for win rate / avg R when there are zero trades, so an em-dash
fallback is needed for those three. Max Drawdown is `0.0` for an empty series
and renders as `+0`.

## Files touched

- `ui/data/strategy_admin.py` — add `start`/`end` params; for each strategy,
  fetch closed trades for the period and merge `compute_kpis` results.
- `ui/tabs/strategies_tab.py` — pass `start, end` from `_render_landing` into
  `_render_admin_panel`; add three columns; widen the layout grid; rename the
  "Total P&L" header to "P&L".
- `tests/test_strategy_admin_state.py` — update data-layer test to the new
  signature (currently calls `get_admin_view(store)` with no period); add
  assertions for the new columns including the None-row em-dash branch.

## Performance

One `trades_repo.get_closed_trades` call per strategy on the landing page (in
addition to the one the cards already make). With ~10 strategies and the
default period (last 7 days) that's 20 small queries; both queries hit the
indexed `(strategy_id, closed_at)` path. If this becomes a hotspot we can
share the dataframe between the admin panel and the cards in a follow-up;
out of scope here.

## Out of scope

- % return (no per-strategy capital base configured).
- Profit factor and expectancy in USD (already on the detail KPI row; not
  requested for the table).
- Sharing the closed-trades dataframe between the admin panel and the cards
  below it.
- Sortable / sticky columns in the table.
