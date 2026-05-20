import os

import yaml

os.environ.setdefault("TRADING_ENV", "test")
os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")

from main import apply_overrides


def test_apply_overrides_returns_cfg_when_path_missing(tmp_path):
    cfg = {"setups": {"price_discovery": {"enabled": True}}}
    out = apply_overrides(cfg, overrides_path=None)
    assert out is cfg
    assert "_per_symbol_overrides" not in out


def test_apply_overrides_returns_cfg_when_file_absent(tmp_path):
    cfg = {"setups": {}}
    out = apply_overrides(cfg, overrides_path=str(tmp_path / "missing.yaml"))
    assert "_per_symbol_overrides" not in out


def test_apply_overrides_loads_per_symbol_map(tmp_path):
    overrides_path = tmp_path / "live_overrides.yaml"
    overrides_path.write_text(yaml.safe_dump({
        "symbols": {
            "AAPL": {
                "timeframe": "15Min",
                "setup": "price_discovery",
                "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                                 "arm_window_bars": 6},
                "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
            },
        },
    }))
    cfg = {"setups": {}}
    out = apply_overrides(cfg, str(overrides_path))
    assert "AAPL" in out["_per_symbol_overrides"]
    assert out["_per_symbol_overrides"]["AAPL"]["timeframe"] == "15Min"


def test_apply_overrides_disabled_flag_short_circuits(tmp_path):
    overrides_path = tmp_path / "live_overrides.yaml"
    overrides_path.write_text(yaml.safe_dump({"symbols": {"AAPL": {}}}))
    cfg = {"setups": {}}
    out = apply_overrides(cfg, str(overrides_path), enabled=False)
    assert "_per_symbol_overrides" not in out


def test_build_setups_uses_override_for_overridden_symbol():
    from main import build_setups
    cfg = {
        "setups": {
            "price_discovery": {"enabled": True, "atr_mult_stop": 0.5,
                                "target_R": 1.0, "arm_window_bars": 6,
                                "cooldown_bars": 12},
            "fade_extreme": {"enabled": True, "atr_mult_stop": 0.75,
                             "scale_offsets_atr": [0.0, 0.25, 0.5],
                             "scale_weights": [0.4, 0.35, 0.25],
                             "cooldown_bars": 12},
            "return_to_value": {"enabled": True, "atr_mult_stop": 1.0,
                                "arm_window_bars": 6, "cooldown_bars": 12},
            "vwap_bounce": {"enabled": True, "atr_mult_stop": 1.25,
                            "target_R": 2.0, "arm_window_bars": 4,
                            "cooldown_bars": 8},
        },
        "_per_symbol_overrides": {
            "AAPL": {
                "timeframe": "15Min",
                "setup": "price_discovery",
                "setup_params": {"atr_mult_stop": 1.25, "target_R": 2.0,
                                 "arm_window_bars": 6},
                "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
            },
        },
    }
    aapl = build_setups(cfg, "AAPL")
    assert len(aapl) == 1
    s = aapl[0]
    assert type(s).__name__ == "PriceDiscoverySetup"
    assert s.atr_mult_stop == 1.25
    assert s.target_R == 2.0

    # Non-overridden symbol still gets all setups with global params
    spy = build_setups(cfg, "SPY")
    assert len(spy) == 4


def test_position_manager_for_uses_override_values():
    from main import position_manager_for
    from state.position_book import PositionBook

    cfg = {
        "position_management": {"max_hold_bars": 12, "breakeven_at_R": 1.0},
        "_per_symbol_overrides": {
            "AAPL": {"timeframe": "15Min", "setup": "price_discovery",
                     "setup_params": {},
                     "position_management": {"max_hold_bars": 8,
                                             "breakeven_at_R": 0.75}},
        },
    }
    book = PositionBook()
    aapl_pm = position_manager_for("AAPL", cfg, book)
    assert aapl_pm._max_hold_bars == 8
    assert aapl_pm._breakeven_at_R == 0.75

    spy_pm = position_manager_for("SPY", cfg, book)
    assert spy_pm._max_hold_bars == 12
    assert spy_pm._breakeven_at_R == 1.0


def test_timeframe_for_resolves_override_then_default():
    from main import timeframe_for
    cfg = {
        "scheduler": {"bar_timeframe": "5Min"},
        "_per_symbol_overrides": {"AAPL": {"timeframe": "15Min"}},
    }
    assert timeframe_for("AAPL", cfg) == "15Min"
    assert timeframe_for("SPY", cfg) == "5Min"


def test_finest_timeframe_picks_shortest_period():
    from main import finest_timeframe
    cfg = {
        "scheduler": {"bar_timeframe": "5Min"},
        "_per_symbol_overrides": {
            "AAPL": {"timeframe": "15Min"},
            "BTC/USD": {"timeframe": "30Min"},
        },
    }
    symbols = [("AAPL", "us_equity"), ("BTC/USD", "crypto"), ("SPY", "us_equity")]
    assert finest_timeframe(symbols, cfg) == "5Min"
