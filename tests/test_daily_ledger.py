from datetime import datetime, timezone
from state.daily_ledger import DailyLedger, TradeRecord


def test_ledger_records_trade_and_streak():
    ledger = DailyLedger(initial_equity=100000.0)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    ledger.record(TradeRecord(symbol="AAPL", setup="price_discovery",
                              entry_ts=t, exit_ts=t, entry_px=100, exit_px=99,
                              side="long", qty=10, R_realized=-1.0, pnl_usd=-10))
    ledger.record(TradeRecord(symbol="AAPL", setup="price_discovery",
                              entry_ts=t, exit_ts=t, entry_px=100, exit_px=99,
                              side="long", qty=10, R_realized=-1.0, pnl_usd=-10))
    assert ledger.consecutive_losses_for("AAPL") == 2
    assert ledger.consecutive_losses_for("MSFT") == 0
    assert ledger.equity == 100000.0 - 20.0


def test_winning_trade_resets_streak():
    ledger = DailyLedger(initial_equity=100000.0)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    ledger.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                              entry_px=100, exit_px=99, side="long", qty=10,
                              R_realized=-1.0, pnl_usd=-10))
    ledger.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                              entry_px=100, exit_px=102, side="long", qty=10,
                              R_realized=2.0, pnl_usd=20))
    assert ledger.consecutive_losses_for("AAPL") == 0


def test_roll_day_clears_streaks():
    ledger = DailyLedger(initial_equity=100000.0)
    t = datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc)
    ledger.record(TradeRecord(symbol="AAPL", setup="x", entry_ts=t, exit_ts=t,
                              entry_px=100, exit_px=99, side="long", qty=10,
                              R_realized=-1.0, pnl_usd=-10))
    ledger.roll_day(datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc))
    assert ledger.consecutive_losses_for("AAPL") == 0
    assert ledger.day_pnl == 0.0
