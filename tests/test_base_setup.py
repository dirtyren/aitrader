import pytest
from datetime import datetime, timezone
from strategies.base_setup import SetupSignal, BaseSetup


def test_setup_signal_dataclass():
    s = SetupSignal(
        setup="price_discovery", symbol="AAPL", side="long",
        entry=100.0, stop=99.0, target=102.0,
        atr=0.5, level=100.5, ts=datetime.now(timezone.utc),
        notes={},
    )
    assert s.r_multiple_target == pytest.approx(2.0, rel=1e-3)
    assert s.risk_per_share == 1.0


def test_base_setup_is_abstract():
    with pytest.raises(TypeError):
        BaseSetup("AAPL")        # cannot instantiate abstract base
