from arbitrage.config import Config


def fee_bps(exchange: str, config: Config) -> float:
    """Return taker fee in basis points (bps) for a given exchange.

    Note: fees are in bps (e.g. 10.0 for 10 bps / 0.10%), NOT fractions.
    """
    if exchange not in config.exchanges:
        raise KeyError(
            f"Unknown exchange '{exchange}'. Available: {list(config.exchanges.keys())}"
        )
    return float(config.exchanges[exchange].taker_fee_bps)


def round_trip_cost_bps(
    config: Config,
    venue_a: str,
    venue_b: str,
    slippage_bps: float,
    leg_risk_prob: float,
    adverse_move_bps: float,
    rebalancing_bps: float,
) -> float:
    """Calculate total round-trip cost in basis points (bps).

    cost = fee(venue_a) + fee(venue_b) + slippage_bps + leg_risk_prob * adverse_move_bps + rebalancing_bps
    All input cost terms are in bps; leg_risk_prob is a probability in [0, 1].
    """
    fee_a = fee_bps(venue_a, config)
    fee_b = fee_bps(venue_b, config)
    return float(
        fee_a
        + fee_b
        + slippage_bps
        + (leg_risk_prob * adverse_move_bps)
        + rebalancing_bps
    )
