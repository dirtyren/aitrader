# Crypto Arbitrage Edge Measurement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained `arbitrage/` package in `aitrader` that measures whether cross-exchange crypto arbitrage has a real, cost-adjusted edge (via free Binance archive + CCXT self-recorder), plus a `test_mode` simulator and a read-only Streamlit dashboard.

**Architecture:** Exchange-agnostic connectors emit a normalized `Quote`; a batch pipeline aligns quotes across venues, computes gross spread, subtracts a cost model (fees + slippage + leg risk + rebalancing), and emits a go/no-go report. A CCXT websocket recorder and a `test_mode` order-sink simulator share the same live-data path, and a separate Streamlit app renders results. No real-order code path exists in v1.

**Tech Stack:** Python ≥3.9 (repo venv is 3.12); pandas 2.2.3; pyarrow 17.0.0 (parquet); requests 2.32.3; pytest 8.3.4; **add `ccxt`** (recorder/paper/live path only); Streamlit 1.41.1 (dashboard).

**Spec:** `docs/superpowers/specs/2026-11-10-crypto-arbitrage-edge-measurement-design.md`

## Global Constraints

- `test_mode` defaults to `true`; `LiveSink.__init__` raises `NotImplementedError`; no real-order code path in v1.
- Quote currency is **USDT-only** for v1; symbol normalization `XBT→BTC`, `-`→`/`. Initial pairs `BTC/USDT`, `ETH/USDT`.
- Alignment grid default **10 ms**; staleness window default **1000 ms**.
- Slippage grid `[0, 1, 2, 5, 10]` bps; leg-risk grid `[0.0, 0.1, 0.5, 1.0]` % of round-trips; latency grid `[10, 50, 100, 250]` ms; reference notional `[10000, 100000]` USD.
- Fees (taker bps, confirm against current published tiers at implementation): binance 10, okx 10, bybit 10, kraken 26, coinbase 60.
- Go/no-go verdict is **"real"** iff median net > 0 at slippage ≥ 1 bp AND median persistence > 1000 ms; else "not real".
- Tests run with `pytest` from the repo root; parquet cache lives under `runtime/bars_cache/arbitrage/` (gitignored).

## Review Focus

1. **Fee-unit confusion (bps vs fraction)** — a 10× error fakes the edge. Pinned in Task 5: `fee_bps("binance") == 10.0` and the round-trip sum is arithmetic in bps.
2. **Spread sign convention flipped** — would report a loss as profit. Pinned in Task 4: `gross_spread` = `bid_sell − ask_buy`, positive ⇔ profitable.
3. **Stale quote fabricating a spread** — a venue whose last quote is old must not look live. Pinned in Task 3: venue with no quote within `staleness_ms` → row dropped.
4. **Clock-skew alignment fabricating a spread** — as-of join must not invent a spread from timestamp offsets. Pinned in Task 3: identical prices + skewed timestamps → aligned gross spread ≈ 0, skew reported.
5. **`test_mode` safety** — flipping the flag must never silently no-op. Pinned in Task 10: `LiveSink()` raises; `test_mode: true` routes to `PaperSink`.

---

### Task 1: Package scaffold, Config, Quote

**Files:**
- Create: `arbitrage/__init__.py`
- Create: `arbitrage/config.yaml`
- Create: `arbitrage/config.py`
- Create: `arbitrage/connectors/__init__.py`
- Create: `arbitrage/connectors/base.py`
- Test: `arbitrage/tests/test_config.py`

**Interfaces:**
- Produces:
  - `Config` (frozen dataclass) with fields exactly: `exchanges: dict[str, ExchangeConfig]`, `pairs: tuple[str, ...]`, `test_mode: bool`, `alignment_grid_ms: int`, `staleness_ms: int`, `slippage_bps_grid: tuple[float, ...]`, `leg_risk_grid: tuple[float, ...]`, `latency_ms_grid: tuple[int, ...]`, `reference_notional_usd: tuple[float, ...]`, `adverse_move_bps: float`, `rebalancing_bps: float`, `go_nogo_min_slippage_bps: float`, `go_nogo_min_persistence_ms: int`.
  - `ExchangeConfig(name: str, taker_fee_bps: float, maker_fee_bps: float = 0.0)`.
  - `load_config(path: str = "arbitrage/config.yaml") -> Config`.
  - `Quote(venue: str, symbol: str, ts_utc_ms: int, bid: float, ask: float)` (frozen dataclass).
  - `VenueConnector` ABC with `load_quotes(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame` returning columns `[ts_utc_ms, bid, ask]` sorted ascending.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_config.py`

```python
def test_load_config_defaults():
    cfg = load_config()
    assert cfg.test_mode is True
    assert cfg.alignment_grid_ms == 10
    assert cfg.staleness_ms == 1000
    assert cfg.slippage_bps_grid == (0, 1, 2, 5, 10)
    assert cfg.pairs == ("BTC/USDT", "ETH/USDT")
    assert cfg.exchanges["binance"].taker_fee_bps == 10.0

def test_config_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_config("arbitrage/does_not_exist.yaml")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: arbitrage.config` / `load_config` undefined.

- [ ] **Step 3: Implement `config.py`, `config.yaml`, `connectors/base.py`, and package `__init__.py`s**

`load_config` parses YAML (via `pyyaml`, already a dependency) into the dataclasses. `config.yaml` contains the Global Constraints values verbatim. `base.py` defines `Quote` and the `VenueConnector` ABC.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/
git commit -m "feat(arbitrage): scaffold package, Config, and Quote/VenueConnector"
```

---

### Task 2: Binance archive downloader

**Files:**
- Create: `arbitrage/connectors/binance_archive.py`
- Create: `arbitrage/tests/fixtures/bookTicker_sample.csv`
- Create: `arbitrage/tests/fixtures/bookTicker_sample.csv.CHECKSUM`
- Test: `arbitrage/tests/test_binance_archive.py`

**Interfaces:**
- Consumes: `Quote`, `VenueConnector` (Task 1).
- Produces:
  - `parse_bookticker_csv(path: Path) -> pd.DataFrame` → `[ts_utc_ms, bid, ask]`.
  - `download_and_cache(symbol: str, market: str, day: date, cache_dir: Path, base_url: str = "https://data.binance.vision") -> Path` — downloads the day's `bookTicker` zip, verifies the adjacent `.CHECKSUM` (sha256), writes parquet, returns the parquet path.
  - `BinanceArchiveConnector(VenueConnector)` with `load_quotes(symbol, market, start, end) -> pd.DataFrame`.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_binance_archive.py`

```python
def test_parse_bookticker_csv():
    df = parse_bookticker_csv(Path("arbitrage/tests/fixtures/bookTicker_sample.csv"))
    assert list(df.columns) == ["ts_utc_ms", "bid", "ask"]
    assert df["ts_utc_ms"].is_monotonic_increasing

def test_checksum_mismatch_raises(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("corrupt")
    # .CHECKSUM present but does not match -> raises
    with pytest.raises(ChecksumMismatchError):
        verify_checksum(bad, tmp_path / "x.csv.CHECKSUM")
```

The fixture CSV uses canonical columns `ts_utc_ms,bid,ask` (mapping from Binance's real header is done by `parse_bookticker_csv` reading the actual header; a helper maps `best bid price`/`best ask price`/`event time` to `bid`/`ask`/`ts_utc_ms`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_binance_archive.py -v`
Expected: FAIL (`parse_bookticker_csv` / `ChecksumMismatchError` undefined).

- [ ] **Step 3: Implement `binance_archive.py`**

`download_and_cache` streams `https://data.binance.vision/data/{market}/daily/bookTicker/{symbol}/{symbol}-bookTicker-{YYYY-MM-DD}.zip`, checks the sibling `.CHECKSUM` file's sha256 against the downloaded bytes, then parses to parquet. Define `ChecksumMismatchError(Exception)` here. `parse_bookticker_csv` maps Binance's real bookTicker columns to `[ts_utc_ms, bid, ask]` (inspect the actual header at execution time; expected fields are best-bid/ask price and event/transaction time).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_binance_archive.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/connectors/binance_archive.py arbitrage/tests/
git commit -m "feat(arbitrage): Binance bookTicker downloader with checksum verify"
```

---

### Task 3: Time alignment

**Files:**
- Create: `arbitrage/align.py`
- Test: `arbitrage/tests/test_align.py`

**Interfaces:**
- Consumes: `pd.DataFrame` with `[ts_utc_ms, bid, ask]` per venue (Tasks 1–2).
- Produces:
  - `align_pair(quotes_by_venue: dict[str, pd.DataFrame], grid_ms: int, staleness_ms: int) -> pd.DataFrame` — index `ts_utc_ms`, columns `bid_<venue>`, `ask_<venue>`; as-of forward-fill; rows where any venue's most-recent quote is older than `staleness_ms` are dropped.
  - `clock_skew_stats(quotes_by_venue: dict[str, pd.DataFrame]) -> dict[str, float]` — per-venue median timestamp delta vs a common reference (median of the first venue's `ts_utc_ms`), for the report.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_align.py`

```python
def test_forward_fill_and_drop_stale():
    a = pd.DataFrame({"ts_utc_ms": [0, 10, 20], "bid": [100, 101, 102], "ask": [100.5, 101.5, 102.5]})
    b = pd.DataFrame({"ts_utc_ms": [0, 25], "bid": [100, 102], "ask": [100.5, 102.5]})
    aligned = align_pair({"a": a, "b": b}, grid_ms=10, staleness_ms=1000)
    # at ts=20, venue b's last quote is at 0 -> age 20ms < 1000 -> kept, forward-filled
    assert aligned.loc[20, "bid_b"] == 100.0

def test_stale_venue_row_dropped():
    a = pd.DataFrame({"ts_utc_ms": [0, 10, 20, 30], "bid": [100]*4, "ask": [101]*4})
    b = pd.DataFrame({"ts_utc_ms": [0], "bid": [100], "ask": [101]})
    aligned = align_pair({"a": a, "b": b}, grid_ms=10, staleness_ms=15)
    assert 20 not in aligned.index  # b stale (>15ms) at ts=20

def test_skew_does_not_fabricate_spread():
    a = pd.DataFrame({"ts_utc_ms": [0, 10], "bid": [100, 100], "ask": [101, 101]})
    b = pd.DataFrame({"ts_utc_ms": [5, 15], "bid": [100, 100], "ask": [101, 101]})  # same prices, 5ms skewed
    aligned = align_pair({"a": a, "b": b}, grid_ms=10, staleness_ms=1000)
    # equal prices -> gross spread ~0 everywhere
    assert (aligned["bid_b"] - aligned["ask_a"]).abs().max() == 0.0
    assert clock_skew_stats({"a": a, "b": b})["b"] == pytest.approx(5.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_align.py -v`
Expected: FAIL (`align_pair` undefined).

- [ ] **Step 3: Implement `align.py`**

Build the grid over `[min_ts, max_ts]` at `grid_ms` steps; for each venue `merge_asof` (forward) its `(ts_utc_ms, bid, ask)` onto the grid; compute age per venue; drop rows where any venue age > `staleness_ms`; rename columns to `bid_<venue>`/`ask_<venue>`. `clock_skew_stats` uses each venue's raw timestamps vs the first venue's median.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_align.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/align.py arbitrage/tests/test_align.py
git commit -m "feat(arbitrage): as-of time alignment with staleness and skew stats"
```

---

### Task 4: Gross spread

**Files:**
- Create: `arbitrage/spread.py`
- Test: `arbitrage/tests/test_spread.py`

**Interfaces:**
- Consumes: aligned DataFrame from Task 3.
- Produces:
  - `gross_spread(aligned: pd.DataFrame, venue_buy: str, venue_sell: str) -> pd.Series` = `aligned["bid_"+venue_sell] - aligned["ask_"+venue_buy]` (quote units; positive ⇔ profitable).
  - `mid_quote(aligned: pd.DataFrame, venue: str) -> pd.Series` = `(bid_<venue> + ask_<venue>) / 2`.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_spread.py`

```python
def test_gross_spread_sign_and_value():
    aligned = pd.DataFrame({"bid_A": [100.0], "ask_A": [100.5], "bid_B": [101.0], "ask_B": [101.5]})
    # buy on A (pay ask_A), sell on B (receive bid_B) -> profit = bid_B - ask_A = 0.5
    assert gross_spread(aligned, "A", "B").iloc[0] == 0.5
    # reverse direction is a loss
    assert gross_spread(aligned, "B", "A").iloc[0] == -1.0

def test_mid_quote():
    aligned = pd.DataFrame({"bid_A": [100.0], "ask_A": [101.0]})
    assert mid_quote(aligned, "A").iloc[0] == 100.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_spread.py -v`
Expected: FAIL (`gross_spread` undefined).

- [ ] **Step 3: Implement `spread.py`**

Two one-line pandas expressions as specified in the signatures.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_spread.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/spread.py arbitrage/tests/test_spread.py
git commit -m "feat(arbitrage): gross spread and mid computation"
```

---

### Task 5: Cost model

**Files:**
- Create: `arbitrage/costs.py`
- Test: `arbitrage/tests/test_costs.py`

**Interfaces:**
- Consumes: `Config` (Task 1).
- Produces:
  - `fee_bps(exchange: str, config: Config) -> float` — taker fee in bps.
  - `round_trip_cost_bps(config: Config, venue_a: str, venue_b: str, slippage_bps: float, leg_risk_prob: float, adverse_move_bps: float, rebalancing_bps: float) -> float` = `fee(a) + fee(b) + slippage + leg_risk_prob*adverse_move + rebalancing`.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_costs.py`

```python
def test_fee_bps_units():
    cfg = load_config()
    assert fee_bps("binance", cfg) == 10.0  # 10 bps, NOT 0.10

def test_round_trip_cost_arithmetic():
    cfg = load_config()
    # binance(10) + okx(10) + slippage 2 + leg_risk 0.5*5 + rebal 2 = 26.5
    cost = round_trip_cost_bps(cfg, "binance", "okx", 2.0, 0.5, 5.0, 2.0)
    assert cost == pytest.approx(26.5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_costs.py -v`
Expected: FAIL (`fee_bps` undefined).

- [ ] **Step 3: Implement `costs.py`**

Read taker fees from `config.exchanges`. `round_trip_cost_bps` is the arithmetic sum above (all terms already in bps).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_costs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/costs.py arbitrage/tests/test_costs.py
git commit -m "feat(arbitrage): fee and round-trip cost model in bps"
```

---

### Task 6: Net edge and statistics

**Files:**
- Create: `arbitrage/net_edge.py`
- Test: `arbitrage/tests/test_net_edge.py`

**Interfaces:**
- Consumes: gross spread + mid (Task 4), cost (Task 5).
- Produces:
  - `net_bps(gross_quote: pd.Series, mid_quote: pd.Series, cost_bps: float) -> pd.Series` = `gross/mid * 1e4 - cost_bps`.
  - `net_edge_stats(net: pd.Series, grid_ms: int) -> dict` with keys `median_bps`, `mean_bps`, `pct_positive`, `persistence_median_ms`, `persistence_max_ms` (persistence = consecutive run length, in ms, of positive values, measured on the `grid_ms` grid).

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_net_edge.py`

```python
def test_net_bps_formula():
    gross = pd.Series([0.5])          # quote units
    mid = pd.Series([100.0])
    # 0.5/100 * 1e4 = 50 bps; minus 10 bps cost -> 40 bps
    assert net_bps(gross, mid, 10.0).iloc[0] == pytest.approx(40.0)

def test_net_edge_stats_persistence():
    net = pd.Series([1.0, 2.0, -1.0, 3.0, 4.0, 5.0])
    s = net_edge_stats(net, grid_ms=10)
    assert s["pct_positive"] == pytest.approx(5/6*100)
    assert s["persistence_max_ms"] == 30  # three consecutive positives * 10ms
    assert s["median_bps"] == pytest.approx(2.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_net_edge.py -v`
Expected: FAIL (`net_bps` undefined).

- [ ] **Step 3: Implement `net_edge.py`**

`net_edge_stats` computes the stats; persistence uses runs of `net > 0` scaled by `grid_ms`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_net_edge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/net_edge.py arbitrage/tests/test_net_edge.py
git commit -m "feat(arbitrage): net edge and distribution statistics"
```

---

### Task 7: Report and go/no-go

**Files:**
- Create: `arbitrage/report.py`
- Test: `arbitrage/tests/test_report.py`

**Interfaces:**
- Consumes: net stats (Task 6), `Config` (Task 1).
- Produces:
  - `go_nogo(median_net_bps: float, persistence_median_ms: int, min_slippage_bps: float, min_persistence_ms: int) -> str` → `"real"` or `"not real"`.
  - `build_report(results: dict, config: Config, json_path: Path, md_path: Path) -> None` — writes the JSON + markdown report; `results` is `{pair: {"venue_buy": ..., "venue_sell": ..., "slippage_bps": {slip: {"median_bps":..., "persistence_median_ms":...}}}}`.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_report.py`

```python
def test_go_nogo_thresholds():
    assert go_nogo(1.5, 2000, 1.0, 1000) == "real"
    assert go_nogo(0.5, 2000, 1.0, 1000) == "not real"
    assert go_nogo(1.5, 500, 1.0, 1000) == "not real"

def test_build_report_writes_files(tmp_path):
    results = {"BTC/USDT": {"slippage_bps": {1.0: {"median_bps": 1.5, "persistence_median_ms": 2000}}}}
    build_report(results, load_config(), tmp_path/"r.json", tmp_path/"r.md")
    assert (tmp_path/"r.json").exists() and (tmp_path/"r.md").exists()
    assert "real" in (tmp_path/"r.md").read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_report.py -v`
Expected: FAIL (`go_nogo` undefined).

- [ ] **Step 3: Implement `report.py`**

`go_nogo` applies the two-threshold rule. `build_report` renders per-pair/per-slippage tables, the verdict line at top, and dumps the same data as JSON.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_report.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/report.py arbitrage/tests/test_report.py
git commit -m "feat(arbitrage): report generation and go/no-go verdict"
```

---

### Task 8: Measurement CLI + synthetic golden test

**Files:**
- Create: `arbitrage/measure.py`
- Test: `arbitrage/tests/test_measure.py`

**Interfaces:**
- Consumes: everything from Tasks 1–7.
- Produces:
  - `measure_pair(pair: str, venues: list[str], config: Config, connector: VenueConnector, start: date, end: date) -> dict` — load → align → gross/mid → sweep slippage+leg-risk+rebalancing → net → stats → per-slippage result dict (the `results` shape Task 7 consumes).
  - `main()` CLI entry (`python -m arbitrage.measure --pair BTC/USDT --venues binance okx ...`).

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_measure.py`

A `FakeConnector` returns synthetic quotes for two venues where venue `sell`'s bid is exactly **5 bps above** venue `buy`'s ask (gross spread 5 bps), with zero fees configured. The golden test asserts the pipeline recovers the injected edge exactly, and that adding fees reduces it by exactly that amount:

```python
class FakeConnector(VenueConnector):
    def __init__(self, frames): self.frames = frames
    def load_quotes(self, symbol, start, end):
        return self.frames[symbol]

def test_recovers_injected_spread_exactly():
    # venue A ask=100.0, venue B bid=100.05 -> gross = 0.05 -> 5 bps
    a = pd.DataFrame({"ts_utc_ms": [0, 10, 20], "bid": [99.9]*3, "ask": [100.0]*3})
    b = pd.DataFrame({"ts_utc_ms": [0, 10, 20], "bid": [100.05]*3, "ask": [100.15]*3})
    cfg = load_config()
    cfg = replace(cfg, exchanges={"A": ExchangeConfig("A", 0.0), "B": ExchangeConfig("B", 0.0)})
    res = measure_pair("X", ["A", "B"], cfg, FakeConnector({"X": {"A": a, "B": b}}), date.today(), date.today())
    # buy on A (ask 100.0), sell on B (bid 100.05) -> gross 5 bps, no cost
    assert res["slippage_bps"][0]["median_bps"] == pytest.approx(5.0)
```

`measure_pair` must accept a mapping of venue→frame; the plan fixes that `measure_pair` takes a `connector` whose `load_quotes` is called per venue (the `FakeConnector` returns the frame for that venue).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_measure.py -v`
Expected: FAIL (`measure_pair` undefined).

- [ ] **Step 3: Implement `measure.py`**

Wire Tasks 1–7 in order. Sweep `slippage_bps_grid × leg_risk_grid` (with `adverse_move_bps` and `rebalancing_bps` from config) per direction (both buy-A/sell-B and buy-B/sell-A), keep the best direction per slippage. `main()` parses args and runs against the real `BinanceArchiveConnector`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_measure.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/measure.py arbitrage/tests/test_measure.py
git commit -m "feat(arbitrage): measurement CLI and synthetic golden test"
```

---

### Task 9: CCXT live recorder

**Files:**
- Create: `arbitrage/connectors/ccxt_venue.py`
- Create: `arbitrage/recorder.py`
- Modify: `requirements.txt` (add `ccxt`)
- Test: `arbitrage/tests/test_recorder.py`

**Interfaces:**
- Consumes: `Config`, `Quote` (Task 1).
- Produces:
  - `CcxtVenue` — wraps a ccxt exchange; `normalize(symbol) -> str` applies `XBT→BTC`, `-`→`/`.
  - `Recorder(config: Config, out_dir: Path)` with `async run()` (subscribes via ccxt.pro `watch_order_book` for each venue×pair, writes `Quote` rows to parquet) and `live_state() -> dict`.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_recorder.py`

Use a fake async exchange (no network) that yields order-book ticks; assert `Recorder` normalizes symbols and persists rows:

```python
def test_normalize_symbol():
    assert CcxtVenue.normalize("XBT/USDT") == "BTC/USDT"
    assert CcxtVenue.normalize("BTC-USDT") == "BTC/USDT"

def test_recorder_writes_quotes(tmp_path):
    # fake exchange yields one book update; recorder.run() appends a Quote row to parquet
    ...
    assert (tmp_path / "binance" / "BTC_USDT.parquet").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_recorder.py -v`
Expected: FAIL (`CcxtVenue` undefined).

- [ ] **Step 3: Implement `recorder.py` + `ccxt_venue.py`**

`recorder.py` runs an asyncio loop over `watch_order_book`; on each update, build a `Quote(venue, symbol, ts_utc_ms, bid, ask)` and append to `out_dir/<venue>/<symbol_slashes_to_underscores>.parquet`. `live_state()` returns per-venue last-quote age and row counts (for the dashboard). Guard the import so the rest of the package works without a network.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_recorder.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/connectors/ccxt_venue.py arbitrage/recorder.py arbitrage/tests/test_recorder.py requirements.txt
git commit -m "feat(arbitrage): ccxt websocket L1 recorder"
```

---

### Task 10: Paper engine (order sink, fill engine, ledger)

**Files:**
- Create: `arbitrage/paper/__init__.py`
- Create: `arbitrage/paper/order_sink.py`
- Create: `arbitrage/paper/fill_engine.py`
- Create: `arbitrage/paper/ledger.py`
- Test: `arbitrage/tests/test_paper.py`

**Interfaces:**
- Consumes: `Signal`, `Fill` (defined here).
- Produces:
  - `Signal(pair, venue_buy, venue_sell, size_usd, gross_bps, ts_utc_ms)` (frozen).
  - `Fill(signal, buy_price, sell_price, fee_usd, pnl_usd, ts_utc_ms)` (frozen).
  - `OrderSink` Protocol with `submit(signal: Signal, book: dict) -> Fill`.
  - `PaperSink(OrderSink)` — fills via `simulate_fill`.
  - `LiveSink(OrderSink)` — `__init__` raises `NotImplementedError`.
  - `simulate_fill(signal, book, latency_ms, slippage_bps, fee_bps) -> Fill` (`book = {"<venue>": {"bid": float, "ask": float}}`).
  - `Ledger` with `record(fill)`, `realized_pnl_usd()`, `round_trips()`.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_paper.py`

```python
def test_live_sink_raises():
    with pytest.raises(NotImplementedError):
        LiveSink()

def test_simulate_fill_applies_slippage():
    signal = Signal("BTC/USDT", "A", "B", 1000.0, 5.0, 0)
    book = {"A": {"bid": 100.0, "ask": 100.5}, "B": {"bid": 101.0, "ask": 101.5}}
    # buy on A: pay ask*(1+slippage); sell on B: receive bid*(1-slippage)
    fill = simulate_fill(signal, book, latency_ms=0, slippage_bps=1.0, fee_bps=0.0)
    assert fill.buy_price == pytest.approx(100.5 * 1.0001)
    assert fill.sell_price == pytest.approx(101.0 * 0.9999)

def test_ledger_pnl():
    led = Ledger()
    led.record(Fill(Signal("BTC/USDT","A","B",1000.0,5.0,0), 100.0, 101.0, 1.0, 5.0, 0))
    assert led.realized_pnl_usd() == 5.0
    assert led.round_trips() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_paper.py -v`
Expected: FAIL (`LiveSink` undefined).

- [ ] **Step 3: Implement the three modules**

`simulate_fill` computes buy at `ask*(1 + slippage_bps/1e4)` and sell at `bid*(1 - slippage_bps/1e4)`, derives notional from `size_usd`, and fills `pnl_usd` and `fee_usd`. `PaperSink.submit` calls it with the venue taker fees. `LiveSink.__init__` raises immediately.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_paper.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/paper/ arbitrage/tests/test_paper.py
git commit -m "feat(arbitrage): paper order sink, fill engine, ledger; live sink stubbed"
```

---

### Task 11: Resource meter + test_mode signal loop + live_state

**Files:**
- Create: `arbitrage/paper/resource_meter.py`
- Create: `arbitrage/paper/sim.py`
- Test: `arbitrage/tests/test_sim.py`

**Interfaces:**
- Consumes: `Config`, `Signal`/`OrderSink`/`Ledger` (Tasks 1, 10), aligned books (Task 3).
- Produces:
  - `ResourceMeter` with `record_quote_latency_ms(venue, latency)`, `record_api_call(venue)`, `record_ws_message(venue)`, and `snapshot() -> dict` (latency percentiles per venue, msg rates, api-call counts, cpu/mem sample).
  - `Simulator(config, sink, meter, ledger, state_path)` with `async run()` — consumes aligned books, emits a `Signal` to `sink` when gross spread (before costs) exceeds a configurable trigger, writes throttled atomic `live_state.json` (current spread, simulated P&L, resource snapshot).

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_sim.py`

```python
def test_resource_meter_snapshot():
    m = ResourceMeter()
    m.record_quote_latency_ms("binance", 12)
    m.record_quote_latency_ms("binance", 20)
    m.record_ws_message("binance")
    s = m.snapshot()
    assert s["binance"]["latency_p50_ms"] == 12.0
    assert s["binance"]["ws_messages"] == 1

def test_sim_writes_live_state_and_uses_paper_sink(tmp_path):
    cfg = load_config()  # test_mode True
    sink = PaperSink(...)
    led = Ledger()
    sim = Simulator(cfg, sink, ResourceMeter(), led, tmp_path/"live_state.json")
    # run one synthetic cycle, assert state file written and no LiveSink used
    ...
    assert (tmp_path/"live_state.json").exists()
    data = json.loads((tmp_path/"live_state.json").read_text())
    assert data["test_mode"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_sim.py -v`
Expected: FAIL (`ResourceMeter` undefined).

- [ ] **Step 3: Implement `resource_meter.py` and `sim.py`**

`Simulator.run()` in test mode builds a `PaperSink` internally if none supplied, never a `LiveSink`. It writes `live_state.json` via write-to-temp-then-`os.replace` (atomic) on a throttle (e.g. 1 Hz).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_sim.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/paper/resource_meter.py arbitrage/paper/sim.py arbitrage/tests/test_sim.py
git commit -m "feat(arbitrage): resource meter and test_mode simulator loop"
```

---

### Task 12: Read-only Streamlit dashboard

**Files:**
- Create: `arbitrage/ui/app.py`
- Create: `arbitrage/ui/__init__.py`
- Test: `arbitrage/tests/test_dashboard.py`

**Interfaces:**
- Consumes: JSON report (Task 7), `live_state.json` (Task 11), parquet recorder output (Task 9).
- Produces: `arbitrage/ui/app.py` Streamlit entry (run: `streamlit run arbitrage/ui/app.py`) with three tabs — **Measurement**, **Live (`test_mode`)**, **Recorder** — all read-only.

- [ ] **Step 1: Write the failing tests** in `arbitrage/tests/test_dashboard.py`

Smoke-test the data-loading helpers (not the Streamlit runtime) that each tab uses:

```python
def test_load_report_and_state(tmp_path):
    build_report({...}, load_config(), tmp_path/"r.json", tmp_path/"r.md")
    (tmp_path/"live_state.json").write_text(json.dumps({"test_mode": True, "pnl_usd": 0.0}))
    rep = load_report(tmp_path/"r.json")
    st = load_live_state(tmp_path/"live_state.json")
    assert st["test_mode"] is True
```

`app.py` must expose `load_report(path)` and `load_live_state(path)` (pure functions) so the tabs are testable without launching Streamlit.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest arbitrage/tests/test_dashboard.py -v`
Expected: FAIL (`load_report` undefined).

- [ ] **Step 3: Implement `ui/app.py`**

Three tabs reading only files (no writes, no ccxt calls): Measurement renders the report tables + verdict; Live renders `live_state.json` (spread, P&L, resource meter); Recorder renders per-venue capture coverage from the parquet dir.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest arbitrage/tests/test_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add arbitrage/ui/ arbitrage/tests/test_dashboard.py
git commit -m "feat(arbitrage): read-only Streamlit dashboard"
```

---

## Self-Review Notes

- **Spec coverage:** every goal (G1–G8) maps to a task: G1→2, G2→9, G3→3, G4→4+5+6, G5→7, G6→10+11, G7→1+2+9, G8→12. The Binance spot-vs-futures basis path reuses Tasks 2–8 with `market="um"` (no new code).
- **Review Focus:** each of the five failure modes is pinned by a named test in its owning task (Task 5 fee units, Task 4 sign, Task 3 staleness + skew, Task 10 `LiveSink`/`test_mode`).
- **Type consistency:** `Quote`, `Config`, `VenueConnector`, `gross_spread`, `net_bps`, `round_trip_cost_bps`, `go_nogo`, `Signal`, `Fill`, `PaperSink`, `LiveSink`, `ResourceMeter`, `Simulator` are used identically across tasks.
