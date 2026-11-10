"""Simulated fill: slippage on both legs, taker fee per leg on notional."""
from __future__ import annotations

from arbitrage.paper.order_sink import Fill, Signal


def simulate_fill(signal: Signal, book: dict, latency_ms: int,
                  slippage_bps: float, fee_bps: float) -> Fill:
    """book = {"<venue>": {"bid": float, "ask": float}}.

    Buy on venue_buy at ask*(1+slip), sell on venue_sell at bid*(1-slip).
    Quantity = size_usd / buy_price. fee_bps is charged per leg on size_usd
    (two legs). pnl_usd is net of fees. Fill ts = signal ts + latency_ms.
    """
    slip = slippage_bps / 1e4
    buy_price = book[signal.venue_buy]["ask"] * (1 + slip)
    sell_price = book[signal.venue_sell]["bid"] * (1 - slip)
    qty = signal.size_usd / buy_price
    fee_usd = 2 * signal.size_usd * fee_bps / 1e4
    pnl_usd = qty * (sell_price - buy_price) - fee_usd
    return Fill(signal, buy_price, sell_price, fee_usd, pnl_usd,
                signal.ts_utc_ms + latency_ms)
