"""
regime_trader — Autonomous Regime-Aware Trading System
"""
import os
import sys

# CRITICAL: Check for emergency lock file before any imports
# (This guard lives here in addition to risk/manager.py for defense-in-depth)
_LOCK_FILE_PATH = os.environ.get("LOCK_FILE_PATH", "lock.file")
_TRADING_ENV = os.environ.get("TRADING_ENV", "production")

if _TRADING_ENV != "test" and os.path.exists(_LOCK_FILE_PATH):
    print("=" * 60)
    print("SYSTEM HALTED: Emergency lock file detected.")
    print(f"Lock file: {os.path.abspath(_LOCK_FILE_PATH)}")
    print("A 10%% peak-to-valley drawdown was detected in a prior session.")
    print("Steps to resume:")
    print("  1. Review the contents of lock.file for incident details")
    print("  2. Perform post-mortem analysis")
    print("  3. Delete lock.file manually")
    print("  4. Restart the system")
    print("=" * 60)
    sys.exit(1)

import logging
import yaml
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

    # Risk
    circuit_breaker = CircuitBreaker(
        peak_equity=100_000.0,  # placeholder; updated at runtime
        daily_loss_limit_1=config["risk"]["daily_loss_limit_1"],
        daily_loss_limit_2=config["risk"]["daily_loss_limit_2"],
        drawdown_limit=config["risk"]["drawdown_limit"],
    )
    risk_manager = RiskManager(
        portfolio_equity=100_000.0,
        circuit_breaker=circuit_breaker,
        max_risk_per_trade=config["strategy"]["max_risk_per_trade"],
    )

    # Broker
    alpaca = AlpacaClient()
    executor = OrderExecutor(alpaca, risk_manager, logger)

    return data_loader, orchestrator, risk_manager, executor, portfolio


def main():
    config = load_config()
    logger = setup_logging(log_file=config["logging"]["log_file"])
    logger.info("regime_trader starting up...")

    # Wire system
    data_loader, orchestrator, risk_manager, executor, portfolio = build_system(config, logger)

    # Connectivity check
    logger.info("Running connectivity handshake...")
    if not executor.connectivity_handshake(config["broker"]["handshake_symbol"]):
        logger.error("Connectivity handshake failed. Aborting.")
        sys.exit(1)

    logger.info("Portfolio loaded: %s", portfolio.tickers)
    logger.info("System ready. Beginning live trading loop.")
    # TODO: implement live trading loop in future milestone


if __name__ == "__main__":
    main()
