from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

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


def test_entry_bar_fetch_uses_consistent_start_across_symbols():
    """All watchlist symbols must be fetched from the same ``since`` cursor in
    one _fetch_entry_bars call.

    The pre-fix bug: last_bar_ts was mutated inside the per-symbol loop, so
    symbol N's get_bars received the cursor already advanced by symbol N-1.
    With AAA returning bars through T1, BBB would have been fetched from T1
    instead of T0 — permanently dropping its T0-to-T1 bars and making it
    unable to trigger during that window.

    This test fails against the pre-fix inline code (BBB start = T1 ≠ T0)
    and passes against _fetch_entry_bars (BBB start = T0 = AAA start).
    """
    from main_opening_drive import _fetch_entry_bars

    T0 = datetime(2026, 8, 28, 14, 0, tzinfo=timezone.utc)   # 10:00 NY as UTC
    T1 = T0 + timedelta(minutes=5)   # latest bar returned by AAA

    calls: dict[str, list] = {}

    class FakeData:
        def get_bars(self, sym, asset_class, timeframe, start, end,
                     use_cache=True):
            calls.setdefault(sym, []).append(start)
            if sym == "AAA":
                bar = MagicMock()
                bar.ts = T1
                return [bar]
            return []  # BBB returns no bars

    loop = MagicMock()
    r_aaa = MagicMock(); r_aaa.symbol = "AAA"
    r_bbb = MagicMock(); r_bbb.symbol = "BBB"
    loop.day.watchlist = [r_aaa, r_bbb]
    loop.data = FakeData()

    fetch_start = T0 - timedelta(minutes=2)
    _fetch_entry_bars(loop, T0 + timedelta(minutes=1), fetch_start)

    # Both symbols must be called with the same start.
    assert "AAA" in calls, "AAA was not fetched at all"
    assert "BBB" in calls, "BBB was not fetched at all"
    assert calls["AAA"] == [fetch_start]
    assert calls["BBB"] == [fetch_start], (
        f"BBB was fetched from {calls['BBB'][0]!r}, expected {fetch_start!r} — "
        "mutating the cursor inside the per-symbol loop drops BBB's "
        f"bars between {fetch_start} and {T1}"
    )
