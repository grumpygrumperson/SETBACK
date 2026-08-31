"""
Tests for the pure parts of metrics.py - the maths that decides the leaderboard.

Run with:  python -m pytest tests/ -q

importorskip on the module: metrics builds a Supabase client at import time,
so these need SUPABASE_URL and SUPABASE_KEY present (any syntactically valid
values will do - nothing here touches the network). The functions under test
are all pure; only the import isn't.
"""

import datetime as dt

import pytest

metrics = pytest.importorskip("metrics")


def ts(day: int, hour: int = 12, month: int = 1) -> int:
    """Millisecond timestamp for a given day in January 2026, UTC."""
    return int(dt.datetime(2026, month, day, hour, tzinfo=dt.timezone.utc).timestamp() * 1000)


def snapshot(day: int, value: float, venue: str = "spot", hour: int = 12) -> dict:
    return {"account_type": venue, "timestamp": ts(day, hour), "total_usdc": value}


# ---------------------------------------------------------------------------
# _period_key / _period_range
# ---------------------------------------------------------------------------

def test_period_key_buckets_by_utc_day():
    assert metrics._period_key(ts(5, hour=0), "daily") == "2026-01-05"
    assert metrics._period_key(ts(5, hour=23), "daily") == "2026-01-05"


def test_period_key_hourly_keeps_the_hour():
    assert metrics._period_key(ts(5, hour=9), "hourly") == "2026-01-05T09"


def test_period_key_rejects_unknown_period():
    with pytest.raises(ValueError, match="Unknown period"):
        metrics._period_key(ts(1), "weekly")


def test_period_range_fills_in_missing_days():
    assert metrics._period_range("2026-01-01", "2026-01-05", "daily") == [
        "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05",
    ]


def test_period_range_single_period():
    assert metrics._period_range("2026-01-01", "2026-01-01", "daily") == ["2026-01-01"]


def test_period_range_crosses_month_boundary():
    keys = metrics._period_range("2026-01-30", "2026-02-02", "daily")
    assert keys == ["2026-01-30", "2026-01-31", "2026-02-01", "2026-02-02"]


@pytest.mark.parametrize("period,first,last", [
    ("daily", "1970-01-01", "2026-01-01"),
    ("hourly", "1970-01-01T00", "2026-01-01T00"),
])
def test_period_range_refuses_an_absurd_span(period, first, last):
    """
    A timestamp stored in seconds rather than milliseconds parses as 1970.
    That should be a readable error, not a silently enormous series.

    Both periods are checked because the daily case is the easier one to miss:
    1970 to now is only ~20,500 days, which any fixed row cap big enough for
    hourly data would let through.
    """
    with pytest.raises(ValueError, match="MILLISECONDS"):
        metrics._period_range(first, last, period)


def test_period_range_allows_a_realistic_competition():
    """The guard must not fire on an ordinary year-long competition."""
    keys = metrics._period_range("2026-01-01", "2026-12-31", "daily")
    assert len(keys) == 365


# ---------------------------------------------------------------------------
# build_portfolio_series
# ---------------------------------------------------------------------------

def test_venues_are_summed_within_a_period():
    """Spot and perp are one competition, written seconds apart."""
    series, _ = metrics.build_portfolio_series([
        snapshot(1, 100.0, "spot", hour=12),
        snapshot(1, 50.0, "perp", hour=12),
    ])
    assert series == [("2026-01-01", 150.0)]


def test_last_snapshot_of_a_period_wins():
    series, _ = metrics.build_portfolio_series([
        snapshot(1, 100.0, "spot", hour=1),
        snapshot(1, 111.0, "spot", hour=23),
    ])
    assert series == [("2026-01-01", 111.0)]


def test_missing_venue_is_carried_forward():
    """
    Day 2 has only spot. Perp's last known value is carried, not dropped -
    summing what's present would read as a crash from 150 to 100.
    """
    series, _ = metrics.build_portfolio_series([
        snapshot(1, 100.0, "spot"), snapshot(1, 50.0, "perp"),
        snapshot(2, 100.0, "spot"),
    ])
    assert series == [("2026-01-01", 150.0), ("2026-01-02", 150.0)]


def test_gap_periods_are_none_not_absent():
    """
    The regression this whole change exists for. Days 4-8 are missing; they
    must appear as None rather than vanish, or day 3 and day 9 become
    adjacent and a five-day move is scored as one daily return.
    """
    series, _ = metrics.build_portfolio_series([
        snapshot(1, 100.0), snapshot(2, 101.0), snapshot(3, 102.0),
        snapshot(9, 110.0), snapshot(10, 111.0),
    ])
    keys = [k for k, _ in series]
    values = [v for _, v in series]

    assert len(series) == 10, "calendar axis must span day 1 to day 10"
    assert keys[3:8] == ["2026-01-04", "2026-01-05", "2026-01-06",
                         "2026-01-07", "2026-01-08"]
    assert values[3:8] == [None] * 5
    assert values[2] == 102.0 and values[8] == 110.0


def test_stale_venue_is_dropped_and_reported_as_an_adjustment():
    """
    A venue silent for longer than max_stale_periods is removed, and the
    removal is reported so it can be treated as a withdrawal rather than a
    trading loss.
    """
    snaps = [snapshot(1, 100.0, "spot"), snapshot(1, 50.0, "perp")]
    snaps += [snapshot(day, 100.0, "spot") for day in range(2, 8)]

    series, adjustments = metrics.build_portfolio_series(snaps, max_stale_periods=3)

    assert sum(adjustments.values()) == -50.0, "the perp value should be backed out"
    assert series[-1][1] == 100.0, "only spot should remain"


def test_staleness_counts_calendar_periods_not_just_observed_ones():
    """
    Previously staleness counted only periods that had data, so a venue could
    stay 'fresh' indefinitely across a total outage. Spot reports on days 1
    and 10; perp only on day 1 and must be stale by day 10.
    """
    series, adjustments = metrics.build_portfolio_series([
        snapshot(1, 100.0, "spot"), snapshot(1, 50.0, "perp"),
        snapshot(10, 100.0, "spot"),
    ], max_stale_periods=3)

    assert adjustments, "perp should have been dropped during the outage"
    assert series[-1][1] == 100.0


def test_empty_input_gives_empty_output():
    assert metrics.build_portfolio_series([]) == ([], {})


def test_snapshots_with_null_value_are_ignored():
    series, _ = metrics.build_portfolio_series([
        {"account_type": "spot", "timestamp": ts(1), "total_usdc": None},
        snapshot(1, 100.0),
    ])
    assert series == [("2026-01-01", 100.0)]


# ---------------------------------------------------------------------------
# period_returns
# ---------------------------------------------------------------------------

def test_returns_between_consecutive_periods():
    series = [("d1", 100.0), ("d2", 110.0), ("d3", 121.0)]
    assert metrics.period_returns(series) == pytest.approx([0.1, 0.1])


def test_deposit_is_not_counted_as_profit():
    """1000 -> 2000 purely by depositing 1000 is a 0% return, not +100%."""
    series = [("d1", 1000.0), ("d2", 2000.0)]
    assert metrics.period_returns(series, {"d2": 1000.0}) == pytest.approx([0.0])


def test_withdrawal_is_not_counted_as_loss():
    series = [("d1", 1000.0), ("d2", 500.0)]
    assert metrics.period_returns(series, {"d2": -500.0}) == pytest.approx([0.0])


def test_returns_never_span_a_gap():
    """
    Both pairs touching a None are dropped. The alternative - joining d1 to
    d3 - would score a two-period move as one period and then annualise it.
    """
    series = [("d1", 100.0), ("d2", None), ("d3", 121.0), ("d4", 133.1)]
    returns = metrics.period_returns(series)
    assert returns == pytest.approx([0.1]), "only the d3->d4 return is well defined"


def test_zero_start_is_skipped_not_divided_by():
    series = [("d1", 0.0), ("d2", 100.0), ("d3", 110.0)]
    assert metrics.period_returns(series) == pytest.approx([0.1])


def test_gap_no_longer_fabricates_volatility():
    """
    The original bug, end to end. Identical underlying performance - a steady
    1%/day - with and without a five-day outage. Both must produce the same
    per-period return, and the gap must not manufacture a spread.
    """
    steady = [snapshot(d, 1000 * (1.01 ** (d - 1))) for d in range(1, 11)]
    with_gap = [s for s in steady if s["timestamp"] not in
                {ts(d) for d in (4, 5, 6, 7, 8)}]

    clean, _ = metrics.build_portfolio_series(steady)
    holed, _ = metrics.build_portfolio_series(with_gap)

    r_clean = metrics.period_returns(clean)
    r_holed = metrics.period_returns(holed)

    assert r_clean == pytest.approx([0.01] * 9)
    assert r_holed == pytest.approx([0.01] * len(r_holed))
    assert max(r_holed) < 0.02, "a five-day move must never appear as one day"


# ---------------------------------------------------------------------------
# mark_internal_transfers
# ---------------------------------------------------------------------------

def flow(timestamp_ms: int, venue: str, direction: str, usdc: float) -> dict:
    return {"account_type": venue, "timestamp": timestamp_ms,
            "direction": direction, "usdc_value": usdc}


def test_matching_pair_across_venues_is_internal():
    flows = metrics.mark_internal_transfers([
        flow(ts(1), "spot", "out", -100.0),
        flow(ts(1) + 60_000, "perp", "in", 100.0),
    ])
    assert all(f["is_internal"] for f in flows)


def test_small_fee_difference_still_pairs():
    """Transfer fees mean the two legs rarely match to the cent."""
    flows = metrics.mark_internal_transfers([
        flow(ts(1), "spot", "out", -100.0),
        flow(ts(1) + 60_000, "perp", "in", 99.0),
    ], tolerance=0.02)
    assert all(f["is_internal"] for f in flows)


def test_pair_outside_the_time_window_is_external():
    flows = metrics.mark_internal_transfers([
        flow(ts(1), "spot", "out", -100.0),
        flow(ts(5), "perp", "in", 100.0),
    ])
    assert not any(f["is_internal"] for f in flows)


def test_same_venue_pair_is_external():
    """A deposit and withdrawal on one venue is real money moving."""
    flows = metrics.mark_internal_transfers([
        flow(ts(1), "spot", "out", -100.0),
        flow(ts(1) + 60_000, "spot", "in", 100.0),
    ])
    assert not any(f["is_internal"] for f in flows)


def test_mismatched_sizes_are_external():
    flows = metrics.mark_internal_transfers([
        flow(ts(1), "spot", "out", -100.0),
        flow(ts(1) + 60_000, "perp", "in", 20.0),
    ])
    assert not any(f["is_internal"] for f in flows)


def test_internal_transfers_are_excluded_from_period_totals():
    flows = metrics.mark_internal_transfers([
        flow(ts(1), "spot", "out", -100.0),
        flow(ts(1) + 60_000, "perp", "in", 100.0),
        flow(ts(1), "spot", "in", 500.0),          # a real deposit
    ])
    totals = metrics.flows_by_period(flows)
    assert totals == pytest.approx({"2026-01-01": 500.0})


# ---------------------------------------------------------------------------
# sharpe_from_returns
# ---------------------------------------------------------------------------

def test_sharpe_of_a_known_series():
    returns = [0.01, -0.005, 0.02, 0.0, 0.015]
    result = metrics.sharpe_from_returns(returns, periods_per_year=365)

    from statistics import mean, stdev
    import math
    expected = (mean(returns) / stdev(returns)) * math.sqrt(365)

    assert result["sharpe"] == pytest.approx(expected)
    assert result["periods"] == 5


def test_constant_returns_give_no_sharpe_rather_than_a_huge_one():
    """
    A perfectly steady series lands at ~1e-16 volatility from floating point,
    not exactly zero - dividing by it would produce a Sharpe around 1e15.
    """
    result = metrics.sharpe_from_returns([0.01] * 10)
    assert result["sharpe"] is None
    assert "volatility" in result["reason"]


def test_flat_account_gives_no_sharpe():
    result = metrics.sharpe_from_returns([0.0] * 10)
    assert result["sharpe"] is None


def test_risk_free_rate_lowers_the_ratio():
    returns = [0.01, -0.005, 0.02, 0.0, 0.015]
    without = metrics.sharpe_from_returns(returns)["sharpe"]
    with_rf = metrics.sharpe_from_returns(returns, risk_free_rate=0.04)["sharpe"]
    assert with_rf < without


def test_too_few_returns_raises():
    with pytest.raises(ValueError, match="at least 2"):
        metrics.sharpe_from_returns([0.01])


def test_sharpe_ratio_needs_three_values():
    with pytest.raises(ValueError, match="at least 3"):
        metrics.sharpe_ratio([100.0, 110.0])


# ---------------------------------------------------------------------------
# Paged reads
#
# PostgREST caps a response at Supabase's `max-rows` and says nothing about
# it - a normal 200 with fewer rows. Snapshots are read oldest-first, so a
# truncated read keeps the OLDEST rows and every Sharpe would be computed
# from a window that stops advancing while still looking plausible.
# ---------------------------------------------------------------------------

class _PagedTable:
    """A query object that serves `rows` in slices, honouring a server cap."""

    def __init__(self, rows, server_cap=None):
        self._rows = rows
        self._cap = server_cap
        self.ranges = []
        self._range = (0, len(rows))

    # the chainable bits metrics.py uses
    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        start, end = self._range
        self.ranges.append((start, end))
        size = end - start + 1
        if self._cap is not None:
            size = min(size, self._cap)
        return type("R", (), {"data": self._rows[start:start + size]})


def _rows(n):
    return [{"i": i} for i in range(n)]


def test_a_single_page_is_read_in_one_request():
    table = _PagedTable(_rows(10))
    assert metrics._fetch_all(lambda: table, "test") == _rows(10)
    assert len(table.ranges) == 2      # the data page, then the empty one


def test_every_row_is_read_when_there_are_several_pages():
    table = _PagedTable(_rows(2500))
    assert metrics._fetch_all(lambda: table, "test") == _rows(2500)


def test_a_server_cap_below_the_page_size_still_reads_everything():
    """
    The subtle one. Stepping by the page size ASKED FOR rather than the rows
    actually returned would skip everything between the server's cap and the
    stride - reading a fraction of the table while looking like a full scan.
    """
    table = _PagedTable(_rows(2500), server_cap=400)
    assert metrics._fetch_all(lambda: table, "test") == _rows(2500)


def test_pages_do_not_overlap_or_leave_holes():
    table = _PagedTable(_rows(2500), server_cap=400)
    metrics._fetch_all(lambda: table, "test")

    starts = [start for start, _ in table.ranges]
    assert starts == sorted(starts)
    assert starts[:4] == [0, 400, 800, 1200]


def test_an_empty_table_reads_cleanly():
    table = _PagedTable([])
    assert metrics._fetch_all(lambda: table, "test") == []


def test_reading_stops_at_the_row_ceiling(monkeypatch):
    """
    A participant with a million snapshots is a bug, not a trader - and
    pulling it would take the whole metrics run down.
    """
    monkeypatch.setattr(metrics, "_MAX_FETCH_ROWS", 2000)
    table = _PagedTable(_rows(100_000))
    assert len(metrics._fetch_all(lambda: table, "test")) == 2000


def test_an_unpriceable_transfer_does_not_take_down_the_participant():
    """
    usdc_value is nullable - a transfer in a currency that couldn't be priced
    stores NULL rather than a wrong number. float(None) would raise inside the
    pairing loop and cost this participant their entire Sharpe over one
    unpriceable transfer.
    """
    flows = [
        {"exchange": "coinbase", "account_type": "spot", "timestamp": ts(1),
         "direction": "out", "usdc_value": None},
        {"exchange": "lighter", "account_type": "perp", "timestamp": ts(1) + 1000,
         "direction": "in", "usdc_value": 100.0},
    ]
    marked = metrics.mark_internal_transfers(flows)
    assert [f["is_internal"] for f in marked] == [False, False]
