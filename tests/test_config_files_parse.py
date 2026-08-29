"""Every strategy config must parse, and the dashboard must see all of them.

Regression guard for f427e17, which removed the circuit_breaker block from all
12 strategy configs with a broken automated edit. In five files it deleted the
newline as well, producing `risk:  consecutive_loss_limit: 3` and
`loss_filter_scope: per_symbolfilters:` — invalid YAML.

Nothing failed loudly. `ui.data.strategy_configs._load` catches YAMLError,
logs it, and CONTINUES, so those five strategies simply vanished from every
dashboard list for months. That silence is what these tests exist to break:
a malformed config must fail a test run, not disappear from a UI.
"""
from __future__ import annotations

import glob
from pathlib import Path

import pytest
import yaml

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"
SETTINGS = sorted(glob.glob(str(CONFIG_DIR / "settings_*.yaml")))


def test_settings_files_found():
    """Guard the guard: a bad glob would make every test below vacuous."""
    assert len(SETTINGS) >= 13, f"expected >=13 strategy configs, found {len(SETTINGS)}"


@pytest.mark.parametrize("path", SETTINGS, ids=lambda p: Path(p).stem)
def test_settings_file_parses(path):
    with open(path) as f:
        loaded = yaml.safe_load(f)
    assert isinstance(loaded, dict), f"{path} did not parse to a mapping"


@pytest.mark.parametrize("path", SETTINGS, ids=lambda p: Path(p).stem)
def test_risk_block_is_a_mapping(path):
    """`risk:  consecutive_loss_limit: 3` is the exact shape f427e17 produced.

    Had the newline landed one line earlier the file would still have parsed,
    with `risk` bound to a string instead of a mapping — so assert the type
    rather than merely that the file loads.
    """
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if "risk" in cfg:
        assert isinstance(cfg["risk"], dict), (
            f"{path}: 'risk' parsed as {type(cfg['risk']).__name__}, not a mapping"
        )


@pytest.mark.parametrize("path", SETTINGS, ids=lambda p: Path(p).stem)
def test_declares_a_system_name(path):
    """Dashboard discovery keys on system.name; a missing one makes the
    strategy indistinguishable from its neighbours."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    assert (cfg.get("system") or {}).get("name"), f"{path}: no system.name"


def test_every_config_is_visible_to_the_dashboard():
    """The end-to-end property that actually broke: a config on disk that the
    dashboard cannot see. Compares discovered names against system.name across
    all files, so a parse failure or a duplicate name fails here."""
    from ui.data.strategy_configs import load_yaml_configs

    on_disk = set()
    for path in SETTINGS:
        with open(path) as f:
            on_disk.add(yaml.safe_load(f)["system"]["name"])

    discovered = set(load_yaml_configs(CONFIG_DIR))
    missing = on_disk - discovered
    assert not missing, f"configs on disk but invisible to the dashboard: {sorted(missing)}"


def test_no_duplicate_system_names():
    """Two files sharing system.name means one is silently shadowed."""
    seen: dict[str, str] = {}
    for path in SETTINGS:
        with open(path) as f:
            name = yaml.safe_load(f)["system"]["name"]
        assert name not in seen, f"{path} and {seen[name]} share system.name {name!r}"
        seen[name] = path
