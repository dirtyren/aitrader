import json
from datetime import datetime, timezone
from pathlib import Path

from state.dashboard_state import DashboardSnapshot, write_dashboard_state


def _snap(symbols=None, rejects=None):
    return DashboardSnapshot(
        timestamp=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc),
        equity=100_100.0,
        day_pnl=100.0,
        circuit_level=0,
        symbols=symbols or [],
        recent_filter_rejects=rejects or [],
    )


def test_write_dashboard_state_round_trips_payload(tmp_path: Path):
    snap = _snap(
        symbols=[
            {"symbol": "AAPL", "vwap": 100.5, "upper": 101.0, "lower": 100.0,
             "last_price": 100.7, "regime": "Range", "open_position": None},
            {"symbol": "BTC/USD", "vwap": 50_100.0, "upper": 50_300.0, "lower": 49_900.0,
             "last_price": 50_050.0, "regime": "Trend",
             "open_position": {"side": "long", "qty": 0.1,
                               "entry": 50_000, "stop": 49_500,
                               "target": 51_000}},
        ],
        rejects=[
            {"filter": "consecutive_loss", "symbol": "AAPL",
             "ts": "2026-05-14T13:55:00+00:00"},
        ],
    )
    out = tmp_path / "state.json"
    write_dashboard_state(out, snap)
    data = json.loads(out.read_text())
    assert data["equity"] == 100_100.0
    assert data["day_pnl"] == 100.0
    assert data["circuit_level"] == 0
    assert len(data["symbols"]) == 2
    assert data["symbols"][0]["last_price"] == 100.7
    assert data["symbols"][1]["last_price"] == 50_050.0
    assert data["symbols"][1]["open_position"]["side"] == "long"
    assert data["recent_filter_rejects"][0]["filter"] == "consecutive_loss"
    assert data["timestamp"] == "2026-05-14T14:00:00+00:00"


def test_write_dashboard_state_creates_parent_dir(tmp_path: Path):
    out = tmp_path / "nested" / "subdir" / "state.json"
    write_dashboard_state(out, _snap())
    assert out.exists()
    assert json.loads(out.read_text())["equity"] == 100_100.0


def test_write_dashboard_state_overwrites_atomically(tmp_path: Path):
    out = tmp_path / "state.json"
    write_dashboard_state(out, _snap())
    second = DashboardSnapshot(
        timestamp=datetime(2026, 5, 14, 14, 5, tzinfo=timezone.utc),
        equity=100_200.0, day_pnl=200.0, circuit_level=1,
        symbols=[], recent_filter_rejects=[],
    )
    write_dashboard_state(out, second)
    data = json.loads(out.read_text())
    assert data["equity"] == 100_200.0
    assert data["circuit_level"] == 1
    # No tmp file lingers after rename
    assert not (tmp_path / "state.json.tmp").exists()
