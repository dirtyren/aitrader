from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml


@dataclass(frozen=True)
class ExchangeConfig:
    name: str
    taker_fee_bps: float
    maker_fee_bps: float = 0.0


@dataclass(frozen=True)
class Config:
    exchanges: dict[str, ExchangeConfig]
    pairs: tuple[str, ...]
    test_mode: bool
    alignment_grid_ms: int
    staleness_ms: int
    slippage_bps_grid: tuple[float, ...]
    leg_risk_grid: tuple[float, ...]
    latency_ms_grid: tuple[int, ...]
    reference_notional_usd: tuple[float, ...]
    adverse_move_bps: float
    rebalancing_bps: float
    go_nogo_min_slippage_bps: float
    go_nogo_min_persistence_ms: int


def load_config(path: str = "arbitrage/config.yaml") -> Config:
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    raw_exchanges = data.get("exchanges", {})
    exchanges: dict[str, ExchangeConfig] = {}
    for key, val in raw_exchanges.items():
        ex_name = val.get("name", key)
        taker_fee = float(val.get("taker_fee_bps", 0.0))
        maker_fee = float(val.get("maker_fee_bps", 0.0))
        exchanges[key] = ExchangeConfig(
            name=ex_name,
            taker_fee_bps=taker_fee,
            maker_fee_bps=maker_fee,
        )

    return Config(
        exchanges=exchanges,
        pairs=tuple(str(p) for p in data.get("pairs", ())),
        test_mode=bool(data.get("test_mode", True)),
        alignment_grid_ms=int(data.get("alignment_grid_ms", 10)),
        staleness_ms=int(data.get("staleness_ms", 1000)),
        slippage_bps_grid=tuple(float(x) for x in data.get("slippage_bps_grid", ())),
        leg_risk_grid=tuple(float(x) for x in data.get("leg_risk_grid", ())),
        latency_ms_grid=tuple(int(x) for x in data.get("latency_ms_grid", ())),
        reference_notional_usd=tuple(
            float(x) for x in data.get("reference_notional_usd", ())
        ),
        adverse_move_bps=float(data.get("adverse_move_bps", 5.0)),
        rebalancing_bps=float(data.get("rebalancing_bps", 2.0)),
        go_nogo_min_slippage_bps=float(data.get("go_nogo_min_slippage_bps", 1.0)),
        go_nogo_min_persistence_ms=int(
            data.get("go_nogo_min_persistence_ms", 1000)
        ),
    )
