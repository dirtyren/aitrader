# Dashboard Remodel — Design Spec

**Date:** 2026-05-28
**Status:** Draft, pending implementation

## Goal

Replace the current Streamlit dashboard with a remodeled version that lets the operator:

1. See per-strategy statistics over a chosen time period (1D, 1W, 15D, 1M, 6M, 1Y, custom).
2. Drill into any strategy to see all trades it took in that period, charts, KPIs, and a full trader-grade info panel.
3. Watch live trading activity (open positions across all strategies) on a dedicated tab.
4. Keep the existing Logs tab for live log inspection.
5. Access the dashboard over HTTPS, gated by HTTP basic authentication (single `admin` user, password from `.env`).

The remodel must look professional — dark mode by default, financial-platform aesthetic.

## Non-Goals

- No Let's Encrypt / public-domain TLS in v1 — self-signed cert is acceptable.
- No multi-user authentication or roles — single `admin` account.
- No write actions from the dashboard (it remains read-only over MySQL and runtime state).
- No MySQL schema changes.
- No changes to how strategies *publish* their state files beyond a single new key (`last_price`) added to each per-symbol row of `trading_state_*.json` — needed so the Live tab can compute unrealized PnL.

## Architecture

```
Browser ──HTTPS (443)──▶ nginx (TLS termination + basic auth)
                            │
                            │ HTTP, internal docker network only
                            ▼
                         Streamlit dashboard ──▶ MySQL (closed trades, open positions, daily_stats)
                                              └─▶ runtime/trading_state_*.json (live regime/equity/last_price)
```

A new `nginx` container is added to `docker-compose.yml`. The Streamlit dashboard container no longer publishes a host port; it is reachable only on the internal docker network. nginx publishes `127.0.0.1:443` (and `127.0.0.1:80` redirects to 443).

## Tabs

1. **Strategies** — landing page. Period selector at the top. One summary card per running strategy: total PnL, win rate, # trades, max drawdown, all over the selected period. Clicking a card selects that strategy and reveals the Strategy Detail view inline (or via a sub-route — Streamlit `st.session_state`).
2. **Strategy Detail** — KPI row, charts, trades table. Bounded by the same period selector. See "Per-strategy stats" below.
3. **Live Trading** — unified open-positions table across all strategies. Auto-refresh every 5 s.
4. **Logs** — kept as-is.
5. **WFO** — kept as-is.

## Period selector

Presets (rolling windows ending now): **1D, 1W, 15D, 1M (30 days), 6M (180 days), 1Y (365 days)**. Plus a **Custom** option that exposes two date pickers (`from`, `to`).

The selector returns a `(start_dt, end_dt)` tuple in UTC. The component lives at `ui/components/period_selector.py` and is unit-testable against a frozen `now`.

The selected period is held in `st.session_state["period"]` so it persists across tab switches and is shared between the Strategies and Strategy Detail views.

## Per-strategy stats

For a `(strategy, start, end)` we compute the following from the `trades` table (rows where `closed_at` falls inside the window):

**KPIs (top metric row):**

- Total PnL (USD)
- # Trades
- Win Rate (%)
- Avg Win / Avg Loss (USD)
- Profit Factor (gross wins / gross losses)
- Expectancy (avg R per trade)
- Max Drawdown (USD, peak-to-trough on the in-period equity curve)
- Sharpe Ratio (daily, annualized × √252)
- Avg Bars Held
- Best Trade / Worst Trade (USD)

**Charts:**

- Equity curve (cumulative PnL over time)
- Daily P&L bar chart (green positive, red negative)
- R-distribution histogram
- Win/loss by setup (grouped bar: count of wins vs losses per `setup_name`)

**Trades table:**

All trades in the window with columns: opened_at, closed_at, symbol, setup, side, qty, entry, exit, stop, target, pnl_usd, R_realized, close_reason, bars_held. Sortable, with a free-text filter on symbol/setup.

## Live Trading tab

Sourced from MySQL `positions WHERE status='open'`, joined to `strategies` for the strategy name.

Columns: Strategy, Symbol, Asset Class, Setup, Side, Qty, Entry, Current Px, Unrealized PnL, R-so-far, Stop, Target, Age (now − opened_at).

`Current Px` and derived columns are computed by reading `runtime/trading_state_*.json` for each strategy. The state file currently publishes `vwap`, `upper`, `lower` per symbol but **not** the last close price; this spec includes a small extension to `_collect_snapshot` in `main.py` to add `last_price` (the last bar's close) to each per-symbol row of the snapshot. This is a single-line addition inside the existing `rows.append({...})` block and is the only producer-side change in this spec. If the state file is missing, stale, or doesn't have a price for the symbol, `Current Px` shows `—` and `Unrealized PnL` / `R-so-far` show `—`. The row is still shown.

Filter chips: select one or more strategies to filter the table.

Auto-refresh: every 5 seconds via `st_autorefresh`.

## Module layout

```
ui/
├── dashboard.py              # entry: page config, theme, tab routing
├── components/
│   ├── __init__.py
│   ├── period_selector.py    # presets + custom range, returns (start, end) UTC
│   ├── strategy_card.py      # KPI summary card for a single strategy
│   └── kpi_row.py            # row of metric tiles
├── tabs/
│   ├── __init__.py
│   ├── strategies_tab.py     # landing cards + per-strategy detail
│   ├── live_tab.py           # open positions across strategies
│   └── logs_panel.py         # MOVED from ui/logs_panel.py, unchanged behavior
├── data/
│   ├── __init__.py
│   ├── trades_repo.py        # closed trades for (strategy, period)
│   ├── positions_repo.py     # open positions across strategies
│   ├── state_files.py        # read trading_state_*.json with safe fallbacks
│   └── stats.py              # pure: KPIs, equity curve, R-hist from trades DataFrame
└── wfo/                      # existing, unchanged
```

`stats.py` takes a trades DataFrame and returns plain Python/pandas results. No Streamlit imports, no DB calls — fully unit-testable.

`trades_repo.py` and `positions_repo.py` use the existing MySQL connection helpers from `state/mysql_store.py` (or a thin read-only wrapper that reuses the same env vars).

## Look & feel — dark mode + financial aesthetic

Default theme is dark. No light-mode toggle in v1.

**Streamlit theme** (`.streamlit/config.toml`):

```toml
[theme]
base = "dark"
primaryColor = "#3b82f6"          # cobalt blue accents
backgroundColor = "#0b0f17"       # near-black with slight blue tint
secondaryBackgroundColor = "#141a26"
textColor = "#e5e7eb"
font = "monospace"                # numeric tables read better in mono
```

**Custom CSS** injected once at startup (`ui/components/theme.py` → `st.markdown(..., unsafe_allow_html=True)`):

- Tabular numbers (`font-variant-numeric: tabular-nums`) on all metric/table values so columns align.
- Positive PnL → `#10b981` (emerald), negative PnL → `#ef4444` (red). Centralized helper `format_pnl(value)` returns colored markdown.
- KPI tiles: subtle border, rounded corners, monospace numbers, label above value.
- Tables: dense rows (Streamlit default is too airy for trader use), tight padding, alternating row striping at very low contrast.
- Charts: dark background, grid lines at low opacity, color palette aligned with the PnL semantics (green/red where it's PnL, neutral blue/grey otherwise).

The intent is "Bloomberg terminal in dark mode, but tasteful" — quiet chrome, loud data.

## Auth + TLS layer

New top-level directory `nginx/`:

```
nginx/
├── Dockerfile           # FROM nginx:alpine + apache2-utils (for htpasswd)
├── entrypoint.sh        # 1) gen self-signed cert if absent, 2) build htpasswd from env
└── nginx.conf           # 80→443 redirect, TLS on 443, basic auth, ws-aware proxy_pass
```

**`entrypoint.sh` outline:**

```sh
#!/bin/sh
set -e

CERT_DIR=/etc/nginx/certs
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out    "$CERT_DIR/fullchain.pem" \
    -subj   "/CN=aitrader-dashboard"
fi

: "${DASH_USER:?DASH_USER is required}"
: "${DASH_PASSWORD:?DASH_PASSWORD is required}"
htpasswd -bc /etc/nginx/.htpasswd "$DASH_USER" "$DASH_PASSWORD"

exec nginx -g 'daemon off;'
```

**`nginx.conf` outline:**

```
server {
  listen 80;
  return 301 https://$host$request_uri;
}

server {
  listen 443 ssl;
  ssl_certificate     /etc/nginx/certs/fullchain.pem;
  ssl_certificate_key /etc/nginx/certs/privkey.pem;

  auth_basic           "aitrader";
  auth_basic_user_file /etc/nginx/.htpasswd;

  location / {
    proxy_pass         http://dashboard:8501;
    proxy_http_version 1.1;
    proxy_set_header   Host $host;
    proxy_set_header   X-Real-IP $remote_addr;
    proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto https;

    # Streamlit websocket
    proxy_set_header   Upgrade $http_upgrade;
    proxy_set_header   Connection "upgrade";
    proxy_read_timeout 86400;
  }
}
```

**`docker-compose.yml` changes:**

- New `nginx` service builds from `./nginx`, env-injects `DASH_USER` and `DASH_PASSWORD`, mounts a named volume `dashboard_certs:/etc/nginx/certs`, publishes `127.0.0.1:443:443` and `127.0.0.1:80:80`, depends on `dashboard`.
- `dashboard` service: drop the `ports: 127.0.0.1:8501:8501` mapping. Container stays on the internal network only. Streamlit still binds `0.0.0.0:8501` inside the container (otherwise nginx can't reach it from another container).

**`.env` additions:**

```
DASH_USER=admin
DASH_PASSWORD=<set-by-operator>
```

`.env` is already gitignored; `config/.env.example` should be updated to include these two keys.

## Data flow summary

- **Strategies / Strategy Detail tabs:** `trades_repo.get_closed_trades(strategy, start, end)` → pandas DataFrame → `stats.compute_kpis(df)` + chart helpers → Streamlit widgets. Pure read against MySQL.
- **Live Trading tab:** `positions_repo.get_open()` (joins `positions` ↔ `strategies`) → DataFrame. For each row, `state_files.get_last_price(strategy, symbol)` looks up `runtime/trading_state_<strategy>.json` and returns the latest known price or `None`. Unrealized PnL = `(current_px − entry_px) × qty × side_sign`, or `None` if price missing.
- **Logs / WFO tabs:** unchanged.

## Error handling

- Repo connection failure → tab shows `st.error("MySQL unreachable: <message>")` and `st.stop()`s that tab only. Other tabs continue to render.
- Empty trade set in period → KPI tiles show `—`, each chart shows `st.info("No trades in this period.")`.
- Missing `trading_state_*.json` or missing price for a symbol on the Live tab → row still rendered with `—` in the price-derived columns.
- nginx: if `DASH_PASSWORD` is unset, `entrypoint.sh` exits with a clear error before `nginx` starts. The container will be in `Restarting` state — visible via `docker compose ps`.

## Testing

**Unit tests (no DB, no network):**

- `tests/ui/test_stats.py` — synthetic trades DataFrames → assert each KPI (PnL, win rate, profit factor, expectancy, Sharpe, max DD) on hand-computed expected values. Edge cases: empty df, all wins, all losses, single trade.
- `tests/ui/test_period_selector.py` — presets resolve to expected `(start, end)` tuples for a frozen `now=2026-05-28T15:00:00Z`. Custom range round-trips.
- `tests/ui/test_state_files.py` — `get_last_price` returns the right value, returns `None` on missing file / missing symbol / malformed JSON.

**Integration tests (real MySQL, matching the project's existing pattern of testing against the real DB):**

- `tests/ui/test_trades_repo.py` — insert known trades into the test DB, query for various windows, assert row counts and ordering.
- `tests/ui/test_positions_repo.py` — open + closed positions seeded, `get_open()` returns only the open ones with strategy name joined.

**Smoke / acceptance:**

- `tests/integration/test_nginx_auth.sh` — bring up the stack, then:
  - `curl -kI https://localhost/` returns `401`.
  - `curl -kI -u admin:wrong https://localhost/` returns `401`.
  - `curl -kI -u admin:$DASH_PASSWORD https://localhost/` returns `200`.
  - `curl -I http://localhost/` returns `301` to https.

Manual acceptance: open `https://localhost/` in a browser, accept the self-signed cert warning, log in, click through each tab, confirm dark theme renders, change the period selector and confirm KPIs update.

## Migration / rollout

- The change is internal: same docker-compose stack, no DB schema change. After `docker compose up --build`, the operator points the browser at `https://localhost/` instead of `http://localhost:8501/` and logs in.
- `runtime/` and `logs/` volumes are untouched, so historical data carries over.
- The old `ui/dashboard.py` is replaced wholesale; no compatibility shim is needed because there are no external consumers of its layout.

## Open questions

None at design time. Any ambiguity that surfaces during implementation should be raised as a plan-level question, not silently decided.
