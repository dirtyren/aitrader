# Crypto Cross-Exchange Arbitrage — Edge Measurement (Design)

**Date:** 2026-11-10
**Status:** Proposed — awaiting approval
**Project:** `aitrader`, new top-level `arbitrage/` package (independent of the existing Alpaca bar-close platform)

## 1. Intent and why

Cross-exchange crypto arbitrage has a plausible-sounding edge (the same coin
trading at different prices on two venues). The question is whether that edge
survives *after costs*: taker/maker fees on both legs, spread, slippage with
size, and the cost of rebalancing inventory between venues.

The purpose of this project is to **answer that question with evidence, not
assertion** — before any capital is committed. A second, co-equal purpose is a
`test_mode` flag that simulates trades and measures the system's resources, so
that live arbitrage can be enabled later by a config flip rather than a rewrite.

The deliverable is a **measurement report**, not a money-making bot.

## 2. Goals, non-goals, anti-goals

### Goals

- **G1 — Fetch free historical L1 data.** Download top-of-book (bid/ask) time
  series for BTC/USDT and ETH/USDT from Binance's free archive
  (`data.binance.vision` `bookTicker`, spot + perpetual-futures), with checksum
  verification and parquet caching.
- **G2 — Record cross-exchange L1 going forward.** A CCXT-based websocket
  recorder that captures L1 from Binance, Coinbase, Kraken, OKX, Bybit to
  parquet, free, starting immediately. This is the only free path to a genuine
  *cross-exchange* dataset (see §3).
- **G3 — Replay and align.** Time-align quotes across venues onto a common
  clock at sub-second resolution (the edge is microstructure-scale).
- **G4 — Compute gross and net spread.** Gross = `bid_B − ask_A` (and the
  reverse leg). Net = gross minus a full cost model (fees both legs, slippage
  as a parameter, amortized rebalancing cost).
- **G5 — Emit a go/no-go report.** Net edge distribution (bps), % of time
  positive, persistence (how long the edge survives), $/day at reference
  capital, and sensitivity to slippage and latency.
- **G6 — `test_mode` simulator.** A live-data-path simulator (no real orders)
  that runs the arbitrage signal over streaming books, simulates fills, and
  instruments resources (latency, message rates, API calls, rate-limit
  headroom, CPU/mem) plus simulated P&L.
- **G7 — Exchange-agnostic connectors.** "Many exchanges" is configuration,
  not new code.
- **G8 — Dashboard.** A self-contained Streamlit app to inspect measurement
  results and live `test_mode` status (see §5.8).

### Non-goals (v1)

- No live trading, no real orders, no real money movement.
- No full L2 depth (deferred — paid or self-recorded; see §3).
- No funding-rate carry or triangular arbitrage (deferred; v1 is spatial
  cross-exchange only).
- No DEXes (CEX-only).
- No integration with the existing bar-close platform's MySQL / reconciler /
  scheduler / Streamlit dashboard. This is a self-contained research subsystem
  with its **own** dashboard (§5.8), which reads the subsystem's parquet/JSON
  outputs rather than MySQL.

### Anti-goals (what makes this a failure even if it works)

- A path that can be flipped to live trading accidentally — `test_mode` must be
  a hard safety default with no live sink in v1.
- A "measurement" that reports **gross** spread as the edge and ignores fees /
  slippage / rebalancing (a false positive).
- Requiring real money or exchange accounts to answer the question.
- Building the full live-trading engine before the measurement says it's
  worth it.

## 3. Binding constraints

### 3.1 Free historical L1 is Binance-only

`data.binance.vision` publishes free, complete, checksummed `bookTicker`
(top-of-book) archives for spot and USDT-M futures. **No other major venue
publishes a free full-history bid/ask archive.** Coinbase, OKX, and Bybit
offer candles/trades for free, not order-book history. True cross-exchange L1
history is paid (Tardis.dev, CryptoStruct) or must be recorded live.

**Consequence:** "start free" splits into two data paths:

1. **Immediate (Binance archive):** full-history L1 for one venue. This cannot
   measure cross-exchange spread, but it *can* measure real spreads that exist
   in the archive today — spot vs perpetual-futures basis, and BTC/USDT vs
   ETH/USDT vs BTC/USDC legs — which fully exercises the pipeline on real data.
2. **Cross-exchange (self-record):** the CCXT recorder (§5.3) captures L1 from
   2–5 venues going forward. Free, but the cross-exchange dataset only becomes
   meaningful after a warm-up period (on the order of weeks).

The design treats **Binance-archive as the correctness path** and
**self-recording as the cross-exchange measurement path**.

### 3.2 The edge is microstructure-scale

Cross-exchange spreads on BTC/ETH are tiny and short-lived (fractions of a
percent for milliseconds-to-seconds). Measurement must preserve sub-second
resolution. Aggregating to candles would hide the entire signal. The alignment
grid (§5.4) defaults to 10 ms and is configurable.

### 3.3 Slippage cannot be measured from L1 alone

Top-of-book data has no depth, so slippage-with-size is an **input parameter**
in v1, not a measurement. It is swept over a grid (0/1/2/5/10 bps) so the
report shows exactly how much slippage kills the edge. Full-depth (L2) replay
is deferred to a later stage, gated on the L1 result.

### 3.4 Venue clock skew and cadence

Venues differ in clock precision and quote update cadence. Alignment must be
robust to skew (each venue's timestamp is treated as its own clock, aligned by
best-effort wall-clock, with skew logged and surfaced in the report rather than
silently assumed away).

## 4. Approaches considered

| Approach | Verdict |
|---|---|
| **A. Offline batch replayer from free archives** (Binance now) + **CCXT self-recorder** (cross-exchange later) | **Chosen.** $0, immediate results on Binance, real cross-exchange data after warm-up. |
| **B. Paid historical data (Tardis.dev) for instant cross-exchange breadth** | Declined for now ("start free"); remains a one-line upgrade if the user wants instant 25+-venue history later. |
| **C. Straight to a paper/live trading bot without measurement** | Rejected — user explicitly chose "measure the edge" first. |
| **Hand-rolled exchange connectors** vs **CCXT** | **CCXT chosen** — unified API over 100+ exchanges; "many exchanges" becomes a config entry. |
| **`test_mode` as a code fork** vs **order-sink abstraction** | **Order-sink chosen** — mirrors the existing repo pattern ("live and backtest share the engine; only the order sink differs"). |

## 5. Design

### 5.1 Package layout

```
arbitrage/
├── config.yaml                  # exchanges, pairs, fee tiers, slippage grid, thresholds, test_mode
├── connectors/
│   ├── base.py                  # VenueConnector interface -> normalized Quote stream
│   ├── binance_archive.py       # downloads data.binance.vision bookTicker (spot + um), checksum verify
│   └── ccxt_venue.py            # live L1 via CCXT websockets (any CCXT-supported venue)
├── recorder.py                  # CCXT ws recorder -> parquet (cross-exchange data path)
├── align.py                     # time alignment: as-of join / forward-fill on a common grid
├── spread.py                    # gross spread: bid_B - ask_A, ask_B - bid_A
├── costs.py                     # fee tiers, slippage parameter, rebalancing amortization
├── net_edge.py                  # net = gross - costs; distribution + persistence stats
├── report.py                    # markdown + JSON report, go/no-go thresholds
├── paper/
│   ├── order_sink.py            # OrderSink protocol; PaperSink (now); LiveSink stub (raises)
│   ├── fill_engine.py           # SimulatedFillEngine (book + latency + slippage)
│   ├── resource_meter.py        # latency histograms, msg rates, API calls, rate-limit headroom, CPU/mem
│   └── ledger.py                # simulated P&L ledger
└── ui/                          # self-contained Streamlit app (measurement + live tabs)
└── tests/                       # unit + fixtures + end-to-end
```

### 5.2 Normalized data model

Every connector emits one normalized record; the rest of the pipeline is
venue-agnostic.

```
Quote(venue, symbol, ts_utc_ms, bid, ask)   # bid_size/ask_size optional
```

Stored as parquet keyed by `(venue, symbol)`. Aligned frame (see §5.4):

```
AlignedFrame(ts_utc_ms, {(venue, symbol): (bid, ask), ...})
```

**Symbol/quote normalization.** Connectors normalize venue-specific
identifiers at the edge: `XBT → BTC`, `-` → `/`, and the quote currency is
**USDT-only for v1**. BTC/USDC and BTC/USD pairs are out of scope for the
cross-exchange set, because mixing USDT/USDC/USD quotes would contaminate the
measured spread with a stablecoin basis. (The Binance-archive BTC/USDC legs in
§5.3 are used only for a same-venue basis sanity check, never as cross-exchange
edge.)

### 5.3 Data acquisition

**Binance archive (immediate).** `binance_archive.py` enumerates
`https://data.binance.vision/data/{spot,um}/daily/bookTicker/{SYMBOL}/`,
downloads `.zip`/`.csv` daily files, verifies the adjacent `.CHECKSUM`
(sha256), and converts to parquet. Symbols: `BTCUSDT`, `ETHUSDT` (spot) and
`BTCUSDT`, `ETHUSDT` (UM futures) for the basis spread.

**CCXT recorder (cross-exchange, ongoing).** `recorder.py` subscribes to public
order-book websockets via CCXT `watchOrderBook` for the configured venues and
pairs, snapshots top-of-book on each update, and appends to a rolling parquet
store. This is the same live feed the `test_mode` simulator consumes, so
recording and paper-trading share one code path.

### 5.4 Alignment

`align.py` joins all venues onto a common 10 ms grid (configurable). For each
grid point, the most recent quote per venue is forward-filled. The join is
`as-of` (each venue's own timestamp) so skew does not fabricate spreads; skew
statistics are computed and reported. The gross spread at time `t` is only
defined when both venues have a quote within a configurable staleness window
(default 1 s).

### 5.5 Cost model

Explicit, and the heart of the measurement:

```
net_bps(t) = (bid_B(t) − ask_A(t)) / mid(t) × 10^4
             − fee_bps(venue_A, leg_buy)
             − fee_bps(venue_B, leg_sell)
             − slippage_bps(size)
             − leg_risk_bps
             − rebalancing_amortized_bps
```

- **Fees** come from `config.yaml` per venue (defaults: Binance 10 bps taker,
  Coinbase/Kraken/OKX/Bybit from their published base tiers; volume tiers are
  config and revisited in the report).
- **Slippage** is a swept parameter (default grid 0/1/2/5/10 bps), not
  measured, per §3.3.
- **Leg risk** (one-sided fill) is a swept parameter, per §5.5.1.
- **Rebalancing** is amortized: the model assumes one-way inventory drift after
  each round-trip, forcing periodic withdrawal+deposit. The per-trade
  amortized cost is a config parameter with a documented default (e.g. flat
  per-transfer fee ÷ expected round-trips between rebalances), swept as a
  secondary parameter.

### 5.5.1 Execution model (pre-funded, simultaneous legs)

The edge is measured under the **pre-funded simultaneous-leg model**: capital
is already sitting on both venues, and both legs are fired at once. This is
the only viable model — the sequential alternative (buy on A, transfer, sell
on B) is killed by transfer latency (see §3). Three execution costs are
therefore modeled explicitly:

- **Slippage with size.** Slippage is tied to a reference notional per fill
  (default **$10k per leg**). The assumption that slippage grows with notional
  tier is documented in `config.yaml` and surfaced in the report, but *not*
  measured (L1 has no depth, §3.3).
- **Leg risk (one-sided fill).** The probability that one leg fills and the
  other misses or slips badly. Modeled as a swept parameter (default grid
  0 / 0.1% / 0.5% / 1% of round-trips) whose cost is
  `leg_risk_prob × typical_adverse_move_bps`. At 0 it is the optimistic
  pure-simultaneous bound; the report shows sensitivity to it.
- **Execution latency.** The recorder/paper sim runs in one process and region
  (not co-located). Detection→submit→fill latency per leg is a swept parameter
  (default grid 10 / 50 / 100 / 250 ms). The report plots net-edge persistence
  against latency — if the median edge dies inside the achievable latency,
  that is a "not real" result regardless of the gross spread.

**Sizing and $/day.** $/day assumes a single round-trip position at a time,
pre-funded, at the reference notional ($10k, also computed at $100k), deployed
only while net > 0. Trade frequency comes from the *measured* occurrence and
persistence, never an assumed rate.

### 5.6 The `test_mode` order-sink

A single global config flag, default **`test_mode: true`**.

- **`true`** → every signal is routed to `PaperSink`, which fills against the
  current book via `SimulatedFillEngine` (applying a configurable execution
  latency and slippage), records the fill in `ledger.py`, and the
  `ResourceMeter` samples latency/messages/API-calls/rate-limit-headroom/CPU/mem
  continuously. No network order is ever sent. The sim also writes a throttled
  atomic `live_state.json` (current spread, simulated P&L, resource snapshots)
  that the dashboard reads.
- **`false`** → routes to `LiveSink`. **In v1, `LiveSink` is a stub that raises
  `NotImplementedError` at construction** — there is no code path that can
  submit a real order. Flipping the flag without implementing the live sink is
  a loud failure, not a silent risk.

### 5.7 Report and go/no-go

The report (markdown + JSON) answers, per pair and per slippage value:

- mean/median net edge (bps), % of time net > 0, histogram of net spread;
- edge **persistence** (how long a positive net edge survives, median/max),
  and **latency sensitivity** (persistence vs the assumed execution latency);
- **leg-risk sensitivity** (net edge vs one-sided-fill probability);
- $/day at reference capital ($10k and $100k), net of costs;
- skew statistics and staleness coverage (honesty about data quality).

Proposed default go/no-go thresholds (builder call, adjustable): the edge is
**"real"** only if, at slippage ≥ 1 bp and the configured fee tier, median net
is positive **and** median persistence exceeds 1 s. Anything else → **"not
real"** (or "marginal"), and the report says so in one line at the top.

### 5.8 Dashboard

A self-contained Streamlit app (`arbitrage/ui/`), separate from the existing
platform dashboard. It reads the arbitrage subsystem's own outputs — parquet,
the JSON report, and the `test_mode` sim's `live_state.json` — and has no MySQL
dependency. It is **read-only**: it cannot place orders or change `test_mode`.

- **Measurement tab:** renders the go/no-go report — net-edge distribution,
  persistence, sensitivity curves (slippage / leg-risk / latency), and the
  per-pair verdict.
- **Live (`test_mode`) tab:** current aligned top-of-book, live gross/net
  spread, simulated P&L ledger, and the resource meter (latency histograms, ws
  message rates, API call counts, rate-limit headroom, CPU/mem).
- **Recorder tab:** per-venue capture status and data coverage (how much L1 has
  been recorded, staleness) — making warm-up progress toward the cross-exchange
  measurement visible.

## 6. How we test it

Two distinct questions, two test layers.

### 6.1 Testing the *measurement system* (correctness)

- **Unit:** fee arithmetic, spread sign (`bid_B − ask_A` vs reverse leg),
  alignment as-of/forward-fill correctness, parquet round-trip, checksum
  verification (tampered file → fail).
- **Synthetic fixtures (the key test):** feed the pipeline quote streams with
  *known injected spreads* (e.g. a constant 5 bp gross spread, zero fees) and
  assert the report recovers exactly 5 bp; then add fees/slippage and assert
  the net matches the formula. This proves the tester isn't lying about its own
  numbers.
- **End-to-end:** download one real day of Binance `bookTicker` for BTC/USDT,
  run the full pipeline, assert the report is non-empty and internally
  consistent (net ≤ gross everywhere).

### 6.2 Testing the *edge* (the actual answer)

- The Binance spot-vs-futures basis measurement is a **known-quantity sanity
  check**: basis is well-understood and typically small/positive — if the
  report says something wildly different, the cost model is wrong.
- The cross-exchange result (after recorder warm-up) is the answer the user
  actually wants; it is reported with its sensitivity curves, not asserted.

## 7. What's left to the builder

- Exact class/method names and internal file details within §5.1's layout.
- The concrete fee-tier defaults in `config.yaml` (pulled from each venue's
  published schedule at implementation time).
- The exact grid/cadence defaults (10 ms alignment, 1 s staleness, slippage /
  leg-risk / latency grids, $10k reference notional) — set in §5, adjustable.
- Report formatting and JSON schema details.

The execution model, sizing, latency, leg-risk, and USDT-only quote-currency
defaults in §5.2 and §5.5.1 are **builder calls** resolving the design review;
the human partner can override any of them.

## 8. Decisions made with the human partner

- **Measure the edge first** (no live trading until evidence says otherwise).
- **Start free** — Binance archive + CCXT self-recorder; paid data is a future
  optional upgrade.
- **Core venues:** Binance, Coinbase, Kraken, OKX, Bybit (tier-2 later:
  Bitfinex, KuCoin, Gate.io, HTX, Bitstamp, Gemini, Crypto.com).
- **Initial pairs:** BTC/USDT, ETH/USDT (then USDT/USDC).
- **`test_mode` flag** with order-sink abstraction and resource measurement.
