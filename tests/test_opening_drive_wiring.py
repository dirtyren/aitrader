from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import yaml

from strategies.opening_drive_scanner import OpeningDriveBaseline

NOW_UTC = datetime(2026, 8, 28, 20, 10, tzinfo=timezone.utc)   # 16:10 NY
BASELINE_FIXTURE = OpeningDriveBaseline(
    atr_14d=4.0, avg_or_volume_20d=50_000.0,
    avg_daily_volume_20d=400_000.0,
    computed_at=NOW_UTC - timedelta(days=1),
)

from main_opening_drive import build_pipeline, load_config, refresh_baselines_post_close
from risk.filters import (
    ConcurrentPositionFilter, ConsecutiveLossFilter, RiskBudgetFilter,
    SectorExposureFilter,
)

CONFIG_PATH = "config/settings_opening_drive_equity.yaml"
SECTORS = {"AAA": "Tech", "BBB": "Energy"}


def test_config_parses():
    cfg = load_config(CONFIG_PATH)
    assert cfg["system"]["name"] == "opening_drive_equity_trader"
    assert cfg["setups"]["opening_drive"]["enabled"] is True


def test_config_declares_equity_asset_class_without_static_symbols():
    """The dashboard discovers strategies by this block; symbols are dynamic."""
    cfg = load_config(CONFIG_PATH)
    assert "equity" in cfg["asset_classes"]
    assert "crypto" not in cfg["asset_classes"]
    assert "symbols" not in cfg["asset_classes"]["equity"]


def test_config_adv_gate_is_iex_denominated():
    """Regression guard: 1_000_000 is a consolidated figure and would reject
    the whole universe on an IEX feed."""
    cfg = load_config(CONFIG_PATH)
    assert cfg["scanner"]["filters"]["min_avg_daily_volume"] == 100_000


def test_config_loss_scope_is_system_wide():
    """per_symbol would never fire — this strategy rotates symbols daily.
    'per_strategy' is not a scope ConsecutiveLossFilter understands."""
    cfg = load_config(CONFIG_PATH)
    assert cfg["risk"]["loss_filter_scope"] == "system_wide"


def test_config_risk_numbers_match_the_capital_split():
    cfg = load_config(CONFIG_PATH)["risk"]
    assert cfg["max_concurrent_positions"] == 5
    assert cfg["max_notional_per_trade_pct"] == 0.07
    assert cfg["max_per_sector"] == 2
    # 5 positions must fit inside the ~40% left by sma_slope's 60%
    assert cfg["max_concurrent_positions"] * cfg["max_notional_per_trade_pct"] <= 0.40


def test_config_max_hold_bars_is_not_inert():
    """11:00->15:30 is 54 five-minute bars; >= 54 would never fire."""
    assert load_config(CONFIG_PATH)["position_management"]["max_hold_bars"] < 54


def test_pipeline_includes_sector_exposure_filter():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS)
    sector = [f for f in pipeline.filters if isinstance(f, SectorExposureFilter)]
    assert len(sector) == 1
    assert sector[0].max_per_sector == 2
    assert sector[0].setup_name == "opening_drive"
    assert sector[0].sector_map == SECTORS


def test_pipeline_consecutive_loss_scope_is_system_wide():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS)
    clf = next(f for f in pipeline.filters
               if isinstance(f, ConsecutiveLossFilter))
    assert clf.scope == "system_wide"
    assert clf.limit == 2


def test_pipeline_includes_concurrency_and_risk_budget():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS)
    cpf = next(f for f in pipeline.filters
               if isinstance(f, ConcurrentPositionFilter))
    assert cpf.max_concurrent == 5
    assert any(isinstance(f, RiskBudgetFilter) for f in pipeline.filters)


def test_pipeline_omits_broker_filters_when_not_supplied():
    cfg = load_config(CONFIG_PATH)
    pipeline = build_pipeline(cfg, SECTORS, alpaca=None, mysql=None)
    names = {f.name for f in pipeline.filters}
    assert "broker_position" not in names
    assert "manual_close_cooldown" not in names


def _loop_stub(baselines_path: str):
    loop = MagicMock()
    loop.scanner.request_symbols.return_value = ["AAA", "SPY"]
    loop.cfg.or_minutes = 30
    loop.cfg.lookback_sessions = 20
    loop.cfg.baselines_path = baselines_path
    return loop


def test_empty_rebuild_does_not_overwrite_existing_baselines(tmp_path):
    """A transient data outage must not erase good baselines and leave the
    next cut with nothing to screen against."""
    path = tmp_path / "baselines.json"
    path.write_text('{"AAA": {"atr_14d": 1.0, "avg_or_volume_20d": 1.0,'
                    ' "avg_daily_volume_20d": 1.0,'
                    ' "computed_at": "2026-08-27T20:10:00Z"}}')
    before = path.read_text()
    loop = _loop_stub(str(path))
    with patch("scripts.build_opening_drive_baselines.build_baselines",
               return_value={}):
        assert refresh_baselines_post_close(loop, NOW_UTC) == 0
    assert path.read_text() == before


def test_successful_rebuild_writes_and_live_reloads(tmp_path):
    path = tmp_path / "baselines.json"
    loop = _loop_stub(str(path))
    built = {"AAA": BASELINE_FIXTURE}
    with patch("scripts.build_opening_drive_baselines.build_baselines",
               return_value=built):
        assert refresh_baselines_post_close(loop, NOW_UTC) == 1
    assert path.exists()
    assert loop.scanner.baselines == built     # no restart required
