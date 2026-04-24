"""
regime_trader — Autonomous Regime-Aware Trading System
"""
import os
import signal
import sys
import json
import time

# CRITICAL: Check for emergency lock file before any imports
# (This guard lives here in addition to risk/manager.py for defense-in-depth)
_LOCK_FILE_PATH = os.environ.get("LOCK_FILE_PATH", "lock.file")
_TRADING_ENV = os.environ.get("TRADING_ENV", "production")

if _TRADING_ENV != "test" and os.path.exists(_LOCK_FILE_PATH):
    print("=" * 60)
    print("SYSTEM HALTED: Emergency lock file detected.")
    print(f"Lock file: {os.path.abspath(_LOCK_FILE_PATH)}")
    print("A 10% peak-to-valley drawdown was detected in a prior session.")
    print("Steps to resume:")
    print("  1. Review the contents of lock.file for incident details")
    print("  2. Perform post-mortem analysis")
    print("  3. Delete lock.file manually")
    print("  4. Restart the system")
    print("=" * 60)
    sys.exit(1)

import logging
import yaml
from datetime import datetime, timezone
from ui.logging_setup import setup_logging
from core.data_loader import DataLoader
from core.feature_eng import build_features
from engine.hmm_model import HMMModel
from engine.regime_classifier import RegimeClassifier
from strategies.vol_allocation import VolatilityAllocationStrategy
from core.orchestrator import StrategyOrchestrator
from risk.circuit_breakers import CircuitBreaker
from risk.manager import RiskManager
from broker.alpaca_client import AlpacaClient
from broker.order_executor import OrderExecutor
from core.portfolio import Portfolio

_STATE_FILE = os.environ.get("STATE_FILE_PATH", "runtime/trading_state.json")
_CYCLE_INTERVAL = int(os.environ.get("CYCLE_INTERVAL_SECONDS", "300"))

_shutdown_requested = False


def _handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def build_system(config: dict, logger: logging.Logger):
    """Wire all components together."""
    # Data
    data_loader = DataLoader(config)
    portfolio = Portfolio.from_config(config)

    # HMM Engine
    hmm_model = HMMModel(
        min_components=config["hmm"]["min_regimes"],
        max_components=config["hmm"]["max_regimes"],
        n_iter=config["hmm"]["n_iter"],
    )
    classifier = RegimeClassifier(hmm_model)

    # Strategy
    strategy = VolatilityAllocationStrategy()
    orchestrator = StrategyOrchestrator(hmm_model, classifier, strategy, logger)

    # Risk (equity placeholder — updated from account after handshake)
    circuit_breaker = CircuitBreaker(
        peak_equity=1.0,
        daily_loss_limit_1=config["risk"]["daily_loss_limit_1"],
        daily_loss_limit_2=config["risk"]["daily_loss_limit_2"],
        drawdown_limit=config["risk"]["drawdown_limit"],
    )
    risk_manager = RiskManager(
        portfolio_equity=1.0,
        circuit_breaker=circuit_breaker,
        max_risk_per_trade=config["strategy"]["max_risk_per_trade"],
        max_rebalance_per_trade=config["strategy"].get("max_rebalance_per_trade", 0.25),
    )

    # Broker
    alpaca = AlpacaClient()
    executor = OrderExecutor(alpaca, risk_manager, logger)

    return data_loader, orchestrator, risk_manager, executor, portfolio, alpaca


def _sync_equity(alpaca: AlpacaClient, risk_manager: RiskManager, logger: logging.Logger) -> float:
    """Fetch real account equity and propagate to risk manager."""
    account = alpaca.get_account()
    equity = float(account.get("equity", 0) or account.get("portfolio_value", 0))
    if equity <= 0:
        raise RuntimeError(f"Account returned invalid equity: {equity}")
    risk_manager.update_equity(equity)
    logger.info("EQUITY_SYNC: equity=%.2f", equity)
    return equity


def _get_current_positions_dollars(alpaca: AlpacaClient) -> dict[str, float]:
    """Return {ticker: market_value} from broker positions."""
    positions = alpaca.get_positions()
    return {
        pos["symbol"]: float(pos.get("market_value", 0))
        for pos in positions
        if pos.get("symbol")
    }


def write_state(
    state_file: str,
    portfolio: Portfolio,
    signal_map: dict,
    equity: float,
    daily_pnl: float,
    current_positions: dict[str, float],
    circuit_level: int,
    drawdown: float,
    trading_suspended: bool,
):
    """Write dashboard state to JSON atomically."""
    portfolio_data = []
    for asset in portfolio.assets:
        sig = signal_map.get(asset.ticker)
        current_weight = current_positions.get(asset.ticker, 0.0) / equity if equity > 0 else 0.0
        portfolio_data.append({
            "ticker": asset.ticker,
            "name": asset.name,
            "target_weight": asset.target_weight,
            "current_weight": current_weight,
            "drift": current_weight - asset.target_weight,
            "regime": sig.regime if sig else "Unknown",
            "confidence": sig.confidence if sig else 0.0,
        })

    state = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "regime": portfolio_data[0]["regime"] if portfolio_data else "Unknown",
        "confidence": portfolio_data[0]["confidence"] if portfolio_data else 0.0,
        "stable": True,
        "n_regimes": 0,
        "circuit_level": circuit_level,
        "drawdown": drawdown,
        "leverage": 1.0,
        "trading_suspended": trading_suspended,
        "signals": [],
        "total_equity": equity,
        "daily_pnl": daily_pnl,
        "prices": [],
        "regimes": [],
        "volumes": [],
        "portfolio": portfolio_data,
    }
    tmp_path = state_file + ".tmp"
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, state_file)


def run_trading_cycle(
    data_loader: DataLoader,
    orchestrator: StrategyOrchestrator,
    risk_manager: RiskManager,
    executor: OrderExecutor,
    portfolio: Portfolio,
    alpaca: AlpacaClient,
    config: dict,
    logger: logging.Logger,
):
    """Execute one full trading cycle: fetch data, detect regimes, rebalance."""
    # 1. Sync equity from broker
    equity = _sync_equity(alpaca, risk_manager, logger)
    start_equity = equity

    # 2. Get current positions
    current_positions = _get_current_positions_dollars(alpaca)

    # 3. Fetch market data and build features for each portfolio asset
    observations_map = {}
    price_data = {}
    for ticker in portfolio.tickers:
        try:
            df = data_loader.fetch_historical(ticker, days=config["hmm"]["train_days"])
            features = build_features(
                df["Close"], df["Volume"],
                window=config["hmm"].get("feature_window", 20),
            )
            if len(features) > 0:
                observations_map[ticker] = features[["log_return", "volatility", "volume_change"]].values
                price_data[ticker] = df["Close"]
        except Exception as exc:
            logger.warning("CYCLE: failed to fetch data for %s: %s", ticker, exc)

    if not observations_map:
        logger.error("CYCLE: no market data available for any ticker — skipping cycle")
        return

    # 4. Fit HMM if not yet fitted, then generate signals
    first_ticker = next(iter(observations_map))
    if orchestrator.hmm_model.model is None:
        logger.info("CYCLE: fitting HMM on %s (%d observations)", first_ticker, len(observations_map[first_ticker]))
        train_features = pd.DataFrame(
            observations_map[first_ticker],
            columns=["log_return", "volatility", "volume_change"],
        )
        orchestrator.hmm_model.fit(train_features)

    signal_map = orchestrator.process_portfolio(observations_map, portfolio)

    # 5. Compute daily P&L
    daily_pnl_pct = (equity - start_equity) / start_equity if start_equity > 0 else 0.0

    # 6. Check if rebalance needed
    if portfolio.needs_rebalance(current_positions, equity):
        logger.info("CYCLE: rebalance triggered")
        results = executor.rebalance_portfolio(
            portfolio=portfolio,
            current_positions=current_positions,
            signal_map=signal_map,
            current_equity=equity,
            daily_pnl_pct=daily_pnl_pct,
        )
        logger.info("CYCLE: rebalance complete — %d order(s)", len(results))
    else:
        logger.info("CYCLE: no rebalance needed")

    # 7. Re-sync equity after trades
    equity = _sync_equity(alpaca, risk_manager, logger)

    # 8. Write state for dashboard
    cb_status = risk_manager.circuit_breaker.check(equity, daily_pnl_pct)
    write_state(
        state_file=_STATE_FILE,
        portfolio=portfolio,
        signal_map=signal_map,
        equity=equity,
        daily_pnl=equity - start_equity,
        current_positions=_get_current_positions_dollars(alpaca),
        circuit_level=cb_status["level"],
        drawdown=risk_manager.circuit_breaker.peak_to_valley_drawdown(equity),
        trading_suspended=cb_status["trading_suspended"],
    )


def main():
    import pandas as pd

    config = load_config()
    logger = setup_logging(log_file=config["logging"]["log_file"])
    logger.info("regime_trader starting up...")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Wire system
    data_loader, orchestrator, risk_manager, executor, portfolio, alpaca = build_system(config, logger)

    # Connectivity check
    logger.info("Running connectivity handshake...")
    if not executor.connectivity_handshake(config["broker"]["handshake_symbol"]):
        logger.error("Connectivity handshake failed. Aborting.")
        sys.exit(1)

    # Sync real account equity before entering the loop
    _sync_equity(alpaca, risk_manager, logger)

    logger.info("Portfolio loaded: %s", portfolio.tickers)
    logger.info("System ready. Beginning live trading loop (interval=%ds).", _CYCLE_INTERVAL)

    while not _shutdown_requested:
        try:
            run_trading_cycle(
                data_loader, orchestrator, risk_manager, executor,
                portfolio, alpaca, config, logger,
            )
        except SystemExit:
            raise
        except Exception as exc:
            logger.error("CYCLE_ERROR: %s", exc, exc_info=True)

        for _ in range(_CYCLE_INTERVAL):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("Shutdown requested — closing all positions...")
    executor.close_all_positions()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
