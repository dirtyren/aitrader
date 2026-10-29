"""Unit tests for EtfRotationBot in main_etf_rotation.py."""

from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest
import yaml

from core.bar import Bar
from main_etf_rotation import EtfRotationBot
from state.position_book import OpenPosition, PositionBook


@pytest.fixture
def dummy_cfg():
    return {
        "system": {
            "name": "etf_rotation_test",
            "version": "1.0.0",
            "trading_env": "paper",
        },
        "scheduler": {
            "bar_timeframe": "1Day",
            "wake_grace_seconds": 0,
            "history_bars": 250,
            "execution_minute_offset": 30,
        },
        "setups": {
            "dual_momentum_etf": {
                "enabled": True,
                "lookback_12w": 60,
                "lookback_24w": 120,
                "weight_12w": 0.60,
                "weight_24w": 0.40,
                "sma_period": 200,
                "vix_defensive_threshold": 22.0,
                "vix_crisis_threshold": 32.0,
                "vix_reentry_threshold": 25.0,
                "consecutive_below_sma_exit": 2,
                "consecutive_above_sma_reenter": 3,
                "turnover_buffer_threshold": 0.01,
            }
        },
        "universe": {
            "satellites": ["SMGB", "QDVE", "EQQQ", "ISPY", "XUFN", "SXLE", "XUIN"],
            "safe_harbor": "CSH2",
            "benchmark": "SPY",
            "vix": "VIX",
        },
        "risk": {
            "max_risk_per_trade": 0.05,
            "max_notional_per_trade_pct": 0.35,
        },
        "backtest": {
            "cache_dir": "runtime/bars_cache",
        },
        "broker": {
            "paper_trading": True,
        },
        "logging": {
            "log_file": "logs/test.log",
        },
    }


def test_bot_initialization(dummy_cfg):
    mock_logger = MagicMock()
    mock_alpaca = MagicMock()
    mock_mysql = MagicMock()
    mock_mysql.load_open_positions.return_value = PositionBook()

    bot = EtfRotationBot(dummy_cfg, mock_logger, alpaca=mock_alpaca, mysql=mock_mysql)
    assert bot.strategy_name == "etf_rotation_test"
    assert bot.benchmark == "SPY"
    assert "SMGB" in bot.satellites


def test_bot_rebalance_execution_sells_and_buys(dummy_cfg):
    mock_logger = MagicMock()
    mock_alpaca = MagicMock()
    mock_alpaca.get_account.return_value = {"equity": "100000.00"}
    mock_alpaca.submit_order.side_effect = lambda **kwargs: {
        "id": "ord_123",
        "filled_avg_price": kwargs.get("limit_price", 100.0),
        "client_order_id": kwargs.get("client_order_id", ""),
    }

    mock_mysql = MagicMock()
    book = PositionBook()
    # Assume we currently hold ISPY (which drops out in target weights)
    ispy_pos = OpenPosition(
        symbol="ISPY",
        setup="dual_momentum_etf",
        side="long",
        qty=100.0,
        entry_px=50.0,
        stop_px=None,
        target_px=None,
        opened_at=datetime.now(timezone.utc),
        order_id="old_ord",
    )
    book.add(ispy_pos)
    mock_mysql.load_open_positions.return_value = book

    bot = EtfRotationBot(dummy_cfg, mock_logger, alpaca=mock_alpaca, mysql=mock_mysql)

    # Target weights: SMGB 35%, QDVE 35%, EQQQ 20%, CSH2 10%, ISPY 0%
    target_weights = {
        "SMGB": 0.35,
        "QDVE": 0.35,
        "EQQQ": 0.20,
        "ISPY": 0.0,
        "CSH2": 0.10,
    }
    latest_prices = {
        "SMGB": 100.0,  # $35,000 -> 350 shares
        "QDVE": 50.0,   # $35,000 -> 700 shares
        "EQQQ": 200.0,  # $20,000 -> 100 shares
        "ISPY": 50.0,   # exit
        "CSH2": 100.0,
    }

    bot._execute_rebalance(target_weights, latest_prices)

    # Check that ISPY was sold
    mock_mysql.position_closed.assert_called_once()
    assert mock_mysql.position_closed.call_args.kwargs["symbol"] == "ISPY"

    # Check that new positions were recorded in MySQL
    assert mock_mysql.position_opened.call_count == 3
    opened_symbols = [call.args[0].symbol for call in mock_mysql.position_opened.call_args_list]
    assert set(opened_symbols) == {"SMGB", "QDVE", "EQQQ"}
