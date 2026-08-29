"""The dashboard discovers strategies by globbing config/settings*.yaml.
Opening Drive has NO static symbols list (the scanner populates it daily), so
this pins that a dynamic-symbol strategy is discovered and rendered without
error rather than being trusted to work by precedent."""
from pathlib import Path

from ui.data.strategy_configs import list_by_asset_class, load_yaml_configs

NAME = "opening_drive_equity_trader"


def test_strategy_is_discovered_from_config_glob():
    assert NAME in load_yaml_configs(Path("config"))


def test_strategy_appears_in_the_equity_list_only():
    assert NAME in list_by_asset_class("equity")
    assert NAME not in list_by_asset_class("crypto")


def test_empty_symbols_parses_to_an_empty_list_not_a_crash():
    cfg = load_yaml_configs(Path("config"))[NAME]
    equity = next(ac for ac in cfg.asset_classes if ac.name == "equity")
    assert equity.symbols == [] or equity.symbols is None


def test_setups_and_risk_are_exposed_to_the_dashboard():
    cfg = load_yaml_configs(Path("config"))[NAME]
    assert [s.name for s in cfg.setups] == ["opening_drive"]
    assert cfg.setups[0].enabled is True
    assert cfg.risk["max_concurrent_positions"] == 5


def test_no_duplicate_strategy_name_conflict():
    """Two YAMLs sharing system.name would make one silently invisible."""
    configs = load_yaml_configs(Path("config"))
    assert configs[NAME].yaml_path.name == "settings_opening_drive_equity.yaml"
