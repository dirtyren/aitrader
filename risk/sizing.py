from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizingConfig:
    max_risk_per_trade: float = 0.005       # fraction of equity
    max_notional_per_trade_pct: float = 0.20
    allow_fractional: bool = False          # crypto = True, equity = False


def size_position(equity: float, entry: float, stop: float, cfg: SizingConfig) -> tuple[float, float]:
    risk_per_share = abs(entry - stop)
    if risk_per_share == 0:
        raise ValueError("Stop distance is zero - cannot size position")
    risk_dollars = equity * cfg.max_risk_per_trade
    raw_qty = risk_dollars / risk_per_share
    raw_notional = raw_qty * entry
    notional_cap = equity * cfg.max_notional_per_trade_pct
    if raw_notional > notional_cap:
        raw_qty = notional_cap / entry
    qty = raw_qty if cfg.allow_fractional else math.floor(raw_qty)
    notional = qty * entry
    return float(qty), float(notional)
