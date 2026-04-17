# regime_trader

## Overview

regime_trader is an autonomous, regime-aware algorithmic trading system that uses a Hidden Markov Model (HMM) to classify the current market regime (Bear, Neutral, Bull, etc.) and dynamically allocates capital based on that classification. The system includes a multi-layered risk management framework with tiered circuit breakers, a walk-forward backtesting engine, and a real-time Streamlit dashboard. It connects to Alpaca Markets for paper and live order execution.

## Architecture

```
regime_trader/
├── config/
│   └── settings.yaml        # All tunable parameters (HMM, risk limits, tickers, broker)
├── core/
│   ├── data_loader.py        # Fetches OHLCV data from Yahoo Finance via yfinance
│   ├── feature_eng.py        # Computes log_return, volatility, volume_change features
│   └── orchestrator.py       # StrategyOrchestrator: wires HMM → strategy → signal
├── engine/
│   ├── hmm_model.py          # GaussianHMM wrapper with BIC-based model selection
│   └── regime_classifier.py  # RegimeClassifier: stability detection, confidence scoring
├── strategies/
│   ├── base_strategy.py      # Abstract BaseStrategy and SignalData dataclass
│   └── vol_allocation.py     # VolatilityAllocationStrategy: regime-to-allocation mapping
├── risk/
│   ├── circuit_breakers.py   # Tiered circuit breaker (3 levels + emergency shutdown)
│   └── manager.py            # RiskManager: veto layer, position sizing, correlation check
├── broker/
│   ├── alpaca_client.py      # Raw HTTP client for Alpaca REST API v2 (retry + auth)
│   └── order_executor.py     # OrderExecutor: signal → risk check → broker order
├── backtest/
│   ├── walk_forward.py       # Walk-forward backtesting engine (train/test splits)
│   ├── performance.py        # Sharpe ratio, max drawdown, CAGR, win-rate metrics
│   └── benchmarks.py         # Buy-and-hold and other benchmark comparisons
├── ui/
│   ├── dashboard.py          # Streamlit dashboard (real-time regime + P&L display)
│   └── logging_setup.py      # Structured logging configuration
├── main.py                   # System entry point — wires all components and starts loop
├── requirements.txt          # Python dependency list
└── README.md                 # This file
```

## System Map

Data flows through the system in a single pipeline per trading cycle:

```
Yahoo Finance
     |
     v
DataLoader.fetch_historical()
     |
     v
build_features()  (log_return, volatility, volume_change)
     |
     v
HMMModel.fit() / predict_current_regime()
     |
     v
RegimeClassifier.update()  (regime name, confidence, stability flag)
     |
     v
StrategyOrchestrator.process()
     |
     v
VolatilityAllocationStrategy.compute_signal()  (allocation_pct, direction)
     |
     v
RiskManager.approve_trade()  (circuit breaker -> position cap -> correlation check)
     |
     v
OrderExecutor.execute_signal()
     |
     v
AlpacaClient.submit_order()  (paper or live)
```

## Setup

### Requirements

- Python 3.11+
- Dependencies listed in `requirements.txt`

Install with:

```bash
pip install -r requirements.txt
```

### Environment Variables

The following environment variables must be set before running the system. Never hardcode credentials.

| Variable           | Required | Default                              | Description                          |
|--------------------|----------|--------------------------------------|--------------------------------------|
| `ALPACA_API_KEY`   | Yes      | —                                    | Alpaca API key ID                    |
| `ALPACA_SECRET_KEY`| Yes      | —                                    | Alpaca secret key                    |
| `ALPACA_BASE_URL`  | No       | `https://paper-api.alpaca.markets`   | Override for live trading endpoint   |
| `TRADING_ENV`      | No       | `production`                         | Set to `test` to bypass lock file check |
| `LOCK_FILE_PATH`   | No       | `lock.file`                          | Path to the emergency lock file      |

Create a `.env` file in the project root (it is loaded automatically via `python-dotenv`):

```bash
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
# ALPACA_BASE_URL=https://api.alpaca.markets  # uncomment for live trading
```

### Logs Directory

The log file path defaults to `logs/regime_trader.log`. Create the directory before first run:

```bash
mkdir -p logs
```

## Configuration

All parameters live in `config/settings.yaml`. Key settings:

| Section    | Key                    | Default | Description                                              |
|------------|------------------------|---------|----------------------------------------------------------|
| `tickers`  | `primary`              | SPY, QQQ, IWM | Tickers used for HMM training and trading          |
| `hmm`      | `train_days`           | 504     | Calendar days of history used to train the HMM (2 years)|
| `hmm`      | `feature_window`       | 20      | Rolling window for volatility feature computation        |
| `hmm`      | `min_regimes`          | 3       | Minimum number of HMM hidden states to evaluate          |
| `hmm`      | `max_regimes`          | 7       | Maximum number of HMM hidden states to evaluate          |
| `hmm`      | `n_iter`               | 100     | EM algorithm iterations per candidate model              |
| `strategy` | `max_risk_per_trade`   | 0.01    | Hard cap: max 1% of portfolio equity per trade           |
| `strategy` | `leverage_bull`        | 1.25    | Allocation multiplier in Bull/Euphoria regimes           |
| `risk`     | `daily_loss_limit_1`   | 0.02    | Level 1 circuit breaker: 2% daily loss → reduce 50%     |
| `risk`     | `daily_loss_limit_2`   | 0.03    | Level 2 circuit breaker: 3% daily loss → halt 24 hours  |
| `risk`     | `drawdown_limit`       | 0.10    | Level 3 circuit breaker: 10% drawdown → emergency halt  |
| `broker`   | `paper_trading`        | true    | Set to false only when ALPACA_BASE_URL points to live    |
| `broker`   | `handshake_symbol`     | NVDA    | Symbol used for the connectivity handshake on startup    |
| `logging`  | `log_file`             | logs/regime_trader.log | Path to the rotating log file               |

## Running

### Main trading system

```bash
python main.py
```

On startup the system will:
1. Check for a lock file (halts if present — see Circuit Breaker section below)
2. Load `config/settings.yaml`
3. Configure structured logging
4. Wire all components
5. Run a connectivity handshake (1-share order placed and immediately cancelled for `handshake_symbol`)
6. Enter the live trading loop

### Streamlit dashboard

```bash
streamlit run ui/dashboard.py
```

The dashboard displays the current regime, confidence score, recent regime history, daily P&L, circuit breaker status, and open positions. It refreshes automatically.

### Backtesting

Run the walk-forward backtest from a Python session or script:

```python
from core.data_loader import DataLoader
from backtest.walk_forward import WalkForwardBacktester
import yaml

with open("config/settings.yaml") as f:
    config = yaml.safe_load(f)

loader = DataLoader(config)
data = loader.fetch_historical("SPY", days=504)

backtester = WalkForwardBacktester(
    train_days=config["backtest"]["train_days"],
    test_days=config["backtest"]["test_days"],
    slippage_bps=config["backtest"]["slippage_bps"],
    commission_bps=config["backtest"]["commission_bps"],
)
results = backtester.run(data)
```

## Circuit Breaker / Lock-File Recovery

### What triggers it

The circuit breaker operates in three escalating levels:

- **Level 1 — Reduce (2% daily loss):** Position sizes are cut by 50% for the remainder of the trading session. Trading continues at reduced size.
- **Level 2 — Halt 24 hours (3% daily loss):** All new order submissions are blocked for 24 hours. Existing positions are not automatically closed.
- **Level 3 — Emergency shutdown (10% peak-to-valley drawdown):** The system immediately closes all open positions, writes a `lock.file` to disk, and calls `sys.exit(1)`. This is a hard stop that survives process restarts.

### What lock.file contains

The lock file is written atomically (via a temp file + `os.replace`) and contains:

```
LOCKED_AT=2026-04-17T14:32:01.123456+00:00
REASON=10% peak-to-valley drawdown threshold breached
```

### Recovery steps

After a Level 3 emergency shutdown the process cannot be restarted until you manually resolve the incident:

1. **Read the lock file** to identify when the shutdown occurred and confirm the reason:
   ```bash
   cat lock.file
   ```

2. **Review the logs** to understand what triggered the drawdown:
   ```bash
   grep -E "REGIME_CHANGE|ORDER_SUBMITTED|CLOSE_POSITION|EMERGENCY" logs/regime_trader.log
   ```

3. **Perform post-mortem analysis.** Check:
   - Which regime sequence preceded the drawdown
   - Whether positions were properly closed (check Alpaca dashboard)
   - Whether the HMM model needs retraining on more recent data
   - Whether risk parameters (drawdown_limit, leverage_bull) need tightening

4. **Delete the lock file manually** once the post-mortem is complete and you are confident it is safe to resume:
   ```bash
   rm lock.file
   ```

5. **Restart the system:**
   ```bash
   python main.py
   ```

Do not delete the lock file without completing the post-mortem. The lock file is your record that an incident occurred.

## HMM Retraining Protocol

### When to retrain

Retrain the HMM model when:
- Performance degrades significantly over a rolling 30-day window (Sharpe < 0.5)
- A major macroeconomic regime shift occurs (e.g., Fed policy reversal, recession onset)
- The model consistently predicts the wrong regime for 5+ consecutive trading days
- Walk-forward backtest results show deteriorating out-of-sample accuracy
- After a Level 3 emergency shutdown as part of the post-mortem

### How to adjust n_regimes

The model selects the optimal number of hidden states automatically using BIC minimisation across the range `[min_regimes, max_regimes]` defined in `settings.yaml`. To adjust:

1. Edit `config/settings.yaml`:
   ```yaml
   hmm:
     min_regimes: 3   # lower bound for BIC search
     max_regimes: 7   # upper bound for BIC search
   ```
2. A wider range (e.g., 3–9) gives BIC more candidates to evaluate but increases training time.
3. Forcing a specific number of regimes: set `min_regimes == max_regimes`.

### Steps to validate a new model

1. **Fetch fresh training data:**
   ```python
   loader = DataLoader(config)
   df = loader.fetch_historical("SPY", days=504)
   ```

2. **Build features and fit:**
   ```python
   from core.feature_eng import build_features
   from engine.hmm_model import HMMModel

   features = build_features(df["Close"], df["Volume"])
   model = HMMModel()
   model.fit(features)
   print(f"Selected n_regimes={model.n_regimes}")
   print(f"Regime labels: {model.regime_labels}")
   ```

3. **Run walk-forward backtest** on the new model and compare Sharpe ratio, max drawdown, and win rate against the prior model.

4. **Inspect regime label quality:** The regime labels should be economically sensible — Bear states should have negative mean log_return, Bull states positive. Check `model.model.means_` to verify.

5. **Save the validated model:**
   ```python
   model.save("models/hmm_model_YYYYMMDD.pkl")
   ```

6. **Deploy** by updating the load path in your run configuration or startup script.

## Troubleshooting

### Missing environment variables

**Symptom:** `KeyError: 'ALPACA_API_KEY'` on startup.

**Fix:** Ensure your `.env` file exists in the project root and contains `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`. Alternatively export them in your shell:
```bash
export ALPACA_API_KEY=your_key
export ALPACA_SECRET_KEY=your_secret
```

### API authentication failure

**Symptom:** `AuthenticationError: Invalid API credentials` in the logs.

**Fix:**
1. Verify your keys at `https://app.alpaca.markets` (paper) or `https://app.alpaca.markets/live`.
2. Ensure `ALPACA_BASE_URL` matches the environment your keys belong to (paper vs live).
3. Check that the keys have not been revoked or regenerated.

### Connectivity handshake failure

**Symptom:** `Connectivity handshake failed. Aborting.` on startup.

**Fix:**
1. Verify API credentials (see above).
2. Check that market hours allow order submission (Alpaca accepts market orders outside hours for paper trading but may reject for live).
3. Verify `handshake_symbol` in `settings.yaml` is a valid, tradeable symbol.
4. Check network connectivity and Alpaca service status at `https://status.alpaca.markets`.

### Lock file present on startup

**Symptom:** `SYSTEM HALTED: Emergency lock file detected.`

**Fix:** Follow the full recovery procedure in the Circuit Breaker / Lock-File Recovery section above. Do not skip the post-mortem.

### Rate limiting from Alpaca

**Symptom:** `RateLimitError: Rate limit exceeded after 5 retries` in the logs.

**Fix:**
1. The client automatically retries with exponential backoff (up to 5 attempts). Persistent rate limiting suggests the trading loop is placing orders too frequently.
2. Add delays between order submissions in the live trading loop.
3. Upgrade your Alpaca plan if you require higher API rate limits.

### yfinance returns empty data

**Symptom:** `ValueError: yfinance returned no data for ticker 'XYZ'`.

**Fix:**
1. Verify the ticker symbol is valid on Yahoo Finance.
2. Try a shorter `days` window — some tickers have limited history.
3. Check your internet connection.
4. Yahoo Finance occasionally rate-limits or returns empty responses. Retry after a short delay.

### HMM fitting fails

**Symptom:** `RuntimeError: HMM fitting failed for all candidate n_components.`

**Fix:**
1. Ensure the training DataFrame has enough rows. With `feature_window=20`, you need at least `20 + min_regimes` rows after `dropna()`.
2. Check for NaN or infinite values in the price or volume series before calling `build_features`.
3. Reduce `min_regimes` to 2 as a diagnostic step to see if smaller models fit.
