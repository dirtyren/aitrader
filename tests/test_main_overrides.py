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
