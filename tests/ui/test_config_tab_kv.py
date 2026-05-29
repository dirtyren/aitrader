"""Coverage for ui.tabs.config_tab helpers that build the KV table.

Regression scope: a mixed-type 'Value' column (bool + int + float + str) used
to trigger Streamlit's Arrow-conversion warning every render. _scalar() must
return a string for every input so the column is uniformly typed.
"""
from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from ui.tabs.config_tab import _scalar


def test_scalar_returns_string_for_every_yaml_scalar():
    assert _scalar(True) == "true"
    assert _scalar(False) == "false"
    assert _scalar(0) == "0"
    assert _scalar(1.5) == "1.5"
    assert _scalar("hello") == "hello"
    assert _scalar(None) == "—"


def test_scalar_dumps_dict_and_list_to_yaml():
    assert _scalar({"a": 1, "b": 2}) == "{a: 1, b: 2}"
    assert _scalar([1, 2, 3]) == "[1, 2, 3]"


def test_kv_table_dataframe_is_arrow_compatible():
    """A representative risk dict (mixed bools, ints, floats, strings) must
    convert to an Arrow table without falling back to object dtype."""
    risk = {
        "max_concurrent_positions": 4,
        "max_risk_per_trade": 0.005,
        "loss_filter_scope": "per_symbol",
        "circuit_breaker_enabled": True,
        "blackout_windows": [],
    }
    rows = [{"Key": k, "Value": _scalar(v)} for k, v in risk.items()]
    df = pd.DataFrame(rows)
    # PyArrow conversion must succeed and the column must come out as a
    # string type — the failure mode this regression test guards against
    # is a mixed-dtype object column that triggers Streamlit's Arrow
    # auto-coercion warning.
    table = pa.Table.from_pandas(df)
    assert pa.types.is_string(table.column("Value").type) or pa.types.is_large_string(table.column("Value").type)
