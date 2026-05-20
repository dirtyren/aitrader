import pytest

from backtest.wfo.grid import ParamCombo, expand_grid


def _grid_spec():
    return {
        "price_discovery": {
            "enabled": [True],
            "atr_mult_stop": [1.0, 1.5],
            "target_R": [1.5, 2.0],
            "cooldown_bars": [12],
        },
        "fade_extreme": {
            "enabled": [False],   # disabled — should produce zero combos
            "atr_mult_stop": [0.75],
            "cooldown_bars": [12],
        },
    }


def _pm_spec():
    return {
        "max_hold_bars": [12, 16],
        "breakeven_at_R": [1.0],
    }


def test_expand_grid_cardinality():
    combos = expand_grid(_grid_spec(), _pm_spec())
    # price_discovery: 1 * 2 * 2 * 1 = 4 setup combos × 2 PM combos = 8
    # fade_extreme: enabled=False → 0 combos
    assert len(combos) == 8
    assert all(c.setup == "price_discovery" for c in combos)


def test_expand_grid_setup_excluded_when_only_disabled():
    spec = {"price_discovery": {"enabled": [False],
                                "atr_mult_stop": [1.0], "cooldown_bars": [12]}}
    assert expand_grid(spec, _pm_spec()) == []


def test_expand_grid_fingerprint_stable_across_input_order():
    spec_a = _grid_spec()
    spec_b = {
        "fade_extreme": _grid_spec()["fade_extreme"],
        "price_discovery": _grid_spec()["price_discovery"],
    }
    fps_a = sorted(c.fingerprint for c in expand_grid(spec_a, _pm_spec()))
    fps_b = sorted(c.fingerprint for c in expand_grid(spec_b, _pm_spec()))
    assert fps_a == fps_b


def test_expand_grid_fingerprint_unique_per_combo():
    combos = expand_grid(_grid_spec(), _pm_spec())
    fps = [c.fingerprint for c in combos]
    assert len(set(fps)) == len(fps)


def test_param_combo_carries_setup_and_pm_values():
    combos = expand_grid(_grid_spec(), _pm_spec())
    c = combos[0]
    assert isinstance(c, ParamCombo)
    assert "atr_mult_stop" in c.setup_values
    assert "target_R" in c.setup_values
    assert "max_hold_bars" in c.pm_values
    assert "breakeven_at_R" in c.pm_values
