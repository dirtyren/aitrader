import os
from datetime import datetime, timezone
from risk.manager import RiskManager
from risk.filters import (
    FilterPipeline, ConcurrentPositionFilter,
    ConsecutiveLossFilter,
)
from risk.sizing import SizingConfig
from state.daily_ledger import DailyLedger
from state.position_book import PositionBook
from strategies.base_setup import SetupSignal


def _signal():
    return SetupSignal(setup="x", symbol="AAPL", side="long",
                       entry=100, stop=99, target=102, atr=1.0,
                       level=100, ts=datetime(2026, 5, 14, 14, 0, tzinfo=timezone.utc))


def _build_rm(ledger, book, lock_path="/nonexistent"):
    pipeline = FilterPipeline([
        ConcurrentPositionFilter(max_concurrent=4),
        ConsecutiveLossFilter(limit=2, scope="per_symbol"),
    ])
    # Notional cap raised to 0.60 so the risk-per-trade limit binds (matches test_sizing fix).
    sizing = SizingConfig(max_risk_per_trade=0.005, max_notional_per_trade_pct=0.60)
    return RiskManager(pipeline=pipeline, sizing_equity=sizing,
                       sizing_crypto=sizing, ledger=ledger, book=book)


def test_evaluate_passes_then_sizes():
    ledger = DailyLedger(initial_equity=100000)
    book = PositionBook()
    rm = _build_rm(ledger, book)
    decision = rm.evaluate(_signal(), ctx=None, asset_class="equity")
    assert decision.approved
    assert decision.qty == 500
    assert decision.notional == 500 * 100


def test_evaluate_rejected_by_concurrent():
    from state.position_book import OpenPosition
    ledger = DailyLedger(initial_equity=100000)
    book = PositionBook()
    for s in ("MSFT", "NVDA", "TSLA", "GOOGL"):
        book.add(OpenPosition(symbol=s, setup="x", side="long", qty=1,
                              entry_px=1, stop_px=0.5, target_px=2,
                              opened_at=datetime.now(timezone.utc), order_id="x"))
    rm = _build_rm(ledger, book)
    decision = rm.evaluate(_signal(), ctx=None, asset_class="equity")
    assert not decision.approved
    assert "concurrent" in decision.reason.lower()
