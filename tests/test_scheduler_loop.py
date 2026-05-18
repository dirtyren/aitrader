from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from core.asset_class import AssetClassConfig
from core.bar import Bar
from core.position_manager import PositionAction
from core.session import SessionContext
from risk.manager import RiskDecision
from scheduler.loop import VWAPWaveEngine
from state.daily_ledger import DailyLedger
from state.position_book import OpenPosition, PositionBook
from strategies.base_setup import SetupSignal


CRYPTO = AssetClassConfig(
    name="crypto", timezone="UTC",
    session_open_local="00:00", session_close_local="23:59",
    opening_blackout_min=15, bar_timeframe="5Min",
    slippage_bps=5.0, commission_per_share=0.0, commission_bps=25.0,
)


def _bar(symbol, ts, c):
    return Bar(symbol=symbol, ts=ts, open=c, high=c + 0.5, low=c - 0.5, close=c, volume=100)


class _Setup:
    """Fires a long signal on the Nth bar."""
    def __init__(self, symbol, fire_on_bar=3):
        self.symbol = symbol
        self.fire_on_bar = fire_on_bar
        self.fired = False

    def check(self, ctx):
        if not self.fired and ctx.bar_count == self.fire_on_bar:
            self.fired = True
            return SetupSignal(setup="fake", symbol=self.symbol, side="long",
                               entry=100, stop=99, target=102, atr=1.0, level=100,
                               ts=ctx.bars[-1].ts)
        return None


def _make_engine(*, executor=None, position_manager=None, risk_manager=None,
                 setup=None, symbol="BTC/USD", asset_class="crypto"):
    book = PositionBook()
    ledger = DailyLedger(initial_equity=100_000)
    if risk_manager is None:
        rm = MagicMock()
        rm.evaluate.return_value = RiskDecision(approved=True, qty=10, notional=1000)
        rm.update_equity = MagicMock()
    else:
        rm = risk_manager
    if position_manager is None:
        pm = MagicMock()
        pm.on_bar.return_value = []
    else:
        pm = position_manager
    ex = executor or MagicMock()
    contexts = {symbol: SessionContext(symbol=symbol, asset_class=CRYPTO)}
    setups = {symbol: [setup or _Setup(symbol)]}
    engine = VWAPWaveEngine(
        symbols=[(symbol, asset_class)],
        contexts=contexts, setups=setups,
        risk_manager=rm, executor=ex, book=book, ledger=ledger,
        position_manager=pm,
    )
    return engine, book, ledger, rm, ex, pm


def _three_bars(symbol="BTC/USD"):
    base = datetime(2026, 5, 14, 0, 5, tzinfo=timezone.utc)
    return [_bar(symbol, base + timedelta(minutes=5 * i), 100 + i) for i in range(3)]


# ---------------------------------------------------------------------------
# Signal → submit
# ---------------------------------------------------------------------------

def test_engine_submits_when_signal_fires_and_risk_approves():
    engine, _, _, rm, ex, _ = _make_engine()
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={"BTC/USD": _three_bars()})
    rm.evaluate.assert_called()
    ex.submit.assert_called_once()


def test_engine_skips_submit_when_risk_rejects():
    rm = MagicMock()
    rm.evaluate.return_value = RiskDecision.reject("blocked")
    rm.update_equity = MagicMock()
    engine, _, _, _, ex, _ = _make_engine(risk_manager=rm)
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={"BTC/USD": _three_bars()})
    ex.submit.assert_not_called()


# ---------------------------------------------------------------------------
# PositionManager actions → executor.handle_actions and ledger
# ---------------------------------------------------------------------------

def _seed_open(book, *, symbol="BTC/USD", entry=100.0, stop=99.0, target=102.0,
               qty=10, side="long", parent_id="parent-1"):
    book.add(OpenPosition(
        symbol=symbol, setup="fake", side=side, qty=qty,
        entry_px=entry, stop_px=stop, target_px=target,
        opened_at=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
        order_id=parent_id, initial_stop_px=stop,
    ))


def test_engine_routes_actions_to_handle_actions_with_parent_id():
    pm = MagicMock()
    pm.on_bar.return_value = [PositionAction(symbol="BTC/USD", kind="stop",
                                             price=99.0, qty=10, side="long")]
    engine, book, _, _, ex, _ = _make_engine(position_manager=pm)
    _seed_open(book, parent_id="parent-1")
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={"BTC/USD": _three_bars()[:1]})
    ex.handle_actions.assert_called_once()
    args, kwargs = ex.handle_actions.call_args
    actions = args[0] if args else kwargs["actions"]
    assert actions[0].kind == "stop"
    assert kwargs.get("asset_class") == "crypto"
    assert kwargs.get("parent_order_id") == "parent-1"


def test_engine_records_trade_on_exit_action():
    pm = MagicMock()
    pm.on_bar.return_value = [PositionAction(symbol="BTC/USD", kind="target",
                                             price=102.0, qty=10, side="long")]
    engine, book, ledger, _, _, _ = _make_engine(position_manager=pm)
    _seed_open(book, entry=100.0, stop=99.0, target=102.0, qty=10)
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={"BTC/USD": _three_bars()[:1]})
    assert len(ledger.trades_today) == 1
    rec = ledger.trades_today[0]
    assert rec.exit_px == 102.0
    assert rec.pnl_usd == (102.0 - 100.0) * 10
    assert rec.R_realized == 2.0   # (102-100) / (100-99)


def test_engine_uses_initial_stop_for_R_after_breakeven_move():
    pm = MagicMock()
    # Position has had stop moved to entry by breakeven, but initial_stop_px preserves R
    pm.on_bar.return_value = [PositionAction(symbol="BTC/USD", kind="time_stop",
                                             price=101.0, qty=10, side="long")]
    engine, book, ledger, _, _, _ = _make_engine(position_manager=pm)
    book.add(OpenPosition(
        symbol="BTC/USD", setup="fake", side="long", qty=10,
        entry_px=100.0, stop_px=100.0, target_px=102.0,
        opened_at=datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc),
        order_id="p", breakeven_moved=True, initial_stop_px=99.0,
    ))
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={"BTC/USD": _three_bars()[:1]})
    rec = ledger.trades_today[0]
    assert rec.R_realized == 1.0   # (101-100) / |100-99|


def test_engine_does_not_record_trade_for_breakeven():
    pm = MagicMock()
    pm.on_bar.return_value = [PositionAction(symbol="BTC/USD", kind="breakeven",
                                             price=100.0, qty=10, side="long")]
    engine, book, ledger, _, ex, _ = _make_engine(position_manager=pm)
    _seed_open(book)
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={"BTC/USD": _three_bars()[:1]})
    assert ledger.trades_today == []
    ex.handle_actions.assert_called_once()


def test_engine_skips_symbol_when_no_fresh_bars():
    engine, _, _, rm, ex, pm = _make_engine()
    engine.tick(now=datetime(2026, 5, 14, 0, 20, tzinfo=timezone.utc),
                fresh_bars={})
    pm.on_bar.assert_not_called()
    rm.evaluate.assert_not_called()
    ex.submit.assert_not_called()
