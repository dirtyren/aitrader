from datetime import datetime, timedelta, timezone

import pytest
from dateutil.relativedelta import relativedelta

from backtest.wfo.windowing import Walk, make_walks, parse_duration


def test_parse_duration_days():
    assert parse_duration("180d") == timedelta(days=180)
    assert parse_duration("1d") == timedelta(days=1)


def test_parse_duration_months():
    assert parse_duration("6mo") == relativedelta(months=6)
    assert parse_duration("12mo") == relativedelta(months=12)


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        parse_duration("6w")
    with pytest.raises(ValueError):
        parse_duration("xmo")
    with pytest.raises(ValueError):
        parse_duration("")


def test_make_walks_rolling_non_overlapping():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 12, 31, tzinfo=timezone.utc)
    walks = make_walks(start, end,
                       in_sample=relativedelta(months=3),
                       out_of_sample=relativedelta(months=1))
    assert all(isinstance(w, Walk) for w in walks)
    # OOS windows must be contiguous and non-overlapping.
    for prev, nxt in zip(walks, walks[1:]):
        assert nxt.oos_start == prev.oos_end
    # Each walk's IS ends where its OOS starts.
    for w in walks:
        assert w.is_end == w.oos_start
    assert walks[0].is_start == start


def test_make_walks_drops_partial_at_end():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 4, 15, tzinfo=timezone.utc)   # not enough for full IS+OOS
    walks = make_walks(start, end,
                       in_sample=timedelta(days=90),
                       out_of_sample=timedelta(days=30))
    # Only walks whose OOS fits before `end` survive
    for w in walks:
        assert w.oos_end <= end


def test_make_walks_step_overlap_allowed():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 6, 1, tzinfo=timezone.utc)
    walks = make_walks(start, end,
                       in_sample=timedelta(days=60),
                       out_of_sample=timedelta(days=30),
                       step=timedelta(days=15))
    # Step < OOS → overlapping OOS windows
    assert walks[1].oos_start < walks[0].oos_end


def test_make_walks_anchored_not_yet_implemented():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 6, 1, tzinfo=timezone.utc)
    with pytest.raises(NotImplementedError):
        make_walks(start, end,
                   in_sample=timedelta(days=60),
                   out_of_sample=timedelta(days=30),
                   anchored=True)
