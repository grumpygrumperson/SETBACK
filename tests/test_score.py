"""
Tests for the scoring layer.

Two things are covered here that were never testable before:

  1. metrics.participant_sharpe — the function that decides the entire
     leaderboard, and which had ZERO tests because the only way to reach it
     was over the network. It now accepts injected snapshots and flows.

  2. score.score_participant — the rule that a row is always written, even
     when there is no score. A participant absent from participant_scores is
     a participant nobody can see is missing.
"""

import datetime as dt

import pytest

metrics = pytest.importorskip("metrics")
score = pytest.importorskip("score")


def ts(day: int, hour: int = 12) -> int:
    """Millisecond timestamp for a day in January 2026, UTC."""
    return int(dt.datetime(2026, 1, day, hour,
                           tzinfo=dt.timezone.utc).timestamp() * 1000)


def snap(day, value, exchange="coinbase", account_type="spot", hour=12):
    return {"participant_id": "p1", "exchange": exchange,
            "account_type": account_type, "timestamp": ts(day, hour),
            "total_usdc": value}


def flow(day, value, direction="in", exchange="coinbase", account_type="spot"):
    return {"participant_id": "p1", "exchange": exchange,
            "account_type": account_type, "timestamp": ts(day, 9),
            "usdc_value": value, "direction": direction,
            "currency": "USDC", "amount": abs(value)}


def rising(days=25, start=1000.0, step=1.01):
    """A steadily compounding account - the well-behaved base case."""
    out, value = [], start
    for d in range(1, days + 1):
        out.append(snap(d, value))
        value *= step
    return out


# ---------------------------------------------------------------------------
# participant_sharpe — first coverage
# ---------------------------------------------------------------------------

def volatile(days=25, start=1000.0):
    """
    An account that actually moves. rising() compounds at a constant rate,
    which has no measurable volatility and therefore no Sharpe - fine as a
    base case, useless for checking a number ever comes out.
    """
    steps = [1.03, 0.98, 1.05, 0.96, 1.02, 1.01, 0.99]
    out, value = [], start
    for d in range(1, days + 1):
        out.append(snap(d, value))
        value *= steps[d % len(steps)]
    return out


def test_a_volatile_series_produces_an_actual_number():
    r = metrics.participant_sharpe("p1", snapshots=volatile(), flows=[])
    assert r["participant_id"] == "p1"
    assert r["periods"] == 24
    assert r["reliable"] is True
    assert isinstance(r["sharpe"], float)
    assert r["volatility"] > 0


def test_the_annualisation_is_reported_with_the_number():
    """A Sharpe figure without its sample size is not interpretable."""
    r = metrics.participant_sharpe("p1", snapshots=volatile(), flows=[])
    assert r["annualisation"] == "sqrt(365)"
    assert r["period"] == "daily"


def test_a_short_history_is_scored_but_flagged_unreliable():
    """
    For the first ~20 days of a 90-day competition nobody reaches 20 returns.
    They must still get a number - ranked below the established, not absent.
    """
    r = metrics.participant_sharpe("p1", snapshots=volatile(days=6), flows=[])
    assert r["periods"] == 5
    assert r["reliable"] is False


def test_perfectly_steady_compounding_has_no_measurable_volatility():
    """
    Not an error. A flat or perfectly constant account has a real answer -
    'there is no volatility to divide by' - and sharpe_from_returns says so
    rather than dividing through to a nonsense 1e15.
    """
    r = metrics.participant_sharpe("p1", snapshots=rising(), flows=[])
    assert r["sharpe"] is None
    assert "volatility" in r["reason"]


def test_too_little_history_raises_rather_than_guessing():
    r = [snap(1, 100), snap(2, 110)]
    with pytest.raises(ValueError, match="at least 3"):
        metrics.participant_sharpe("p1", snapshots=r, flows=[])


def test_no_history_at_all_raises():
    with pytest.raises(ValueError, match="at least 3"):
        metrics.participant_sharpe("p1", snapshots=[], flows=[])


def test_a_deposit_does_not_count_as_performance():
    """
    The correction the whole module exists for. An account that goes
    1000 -> 2000 purely by depositing 1000 has earned nothing.
    """
    snapshots = [snap(1, 1000), snap(2, 1000), snap(3, 2000), snap(4, 2000)]
    funded = metrics.participant_sharpe(
        "p1", snapshots=snapshots, flows=[flow(3, 1000.0)])
    unfunded = metrics.participant_sharpe("p1", snapshots=snapshots, flows=[])

    assert funded["external_flow_usdc"] == 1000.0
    assert unfunded["external_flow_usdc"] == 0.0
    # The deposit day is a 0% return once adjusted, but +100% if not.
    assert max(abs(x) for x in [funded["mean_return"]]) < abs(unfunded["mean_return"])


def test_spot_and_perp_are_summed_into_one_portfolio():
    """One competition, written seconds apart under different timestamps."""
    snapshots = []
    for d in range(1, 6):
        snapshots.append(snap(d, 100, account_type="spot", hour=12))
        snapshots.append(snap(d, 50, account_type="perp", hour=12))
    r = metrics.participant_sharpe("p1", snapshots=snapshots, flows=[])
    assert r["observed_periods"] == 5


def test_a_transfer_between_own_venues_is_netted_out():
    """
    Moving collateral from Coinbase spot to Lighter perp changes nothing about
    a participant's standing, but the exchanges report it twice.
    """
    snapshots = [snap(d, 1000) for d in range(1, 6)]
    flows = [
        flow(3, -500.0, direction="out", exchange="coinbase", account_type="spot"),
        flow(3, 500.0, direction="in", exchange="lighter", account_type="perp"),
    ]
    r = metrics.participant_sharpe("p1", snapshots=snapshots, flows=flows)
    assert r["internal_transfers"] == 2
    assert r["external_flow_usdc"] == 0.0


def test_injected_data_is_used_instead_of_the_network():
    """
    The property that makes every test above possible. If this ever regresses
    the suite would start making live Supabase calls, which is the state the
    function was in before.
    """
    called = []
    original = metrics.fetch_snapshots
    metrics.fetch_snapshots = lambda pid: called.append(pid) or []
    try:
        metrics.participant_sharpe("p1", snapshots=rising(), flows=[])
    finally:
        metrics.fetch_snapshots = original
    assert called == []


# ---------------------------------------------------------------------------
# score_participant — always a row
# ---------------------------------------------------------------------------

def test_a_scored_participant_gets_their_numbers():
    row = score.score_participant("p1", "daily", rising(), [])
    assert row["participant_id"] == "p1"
    assert row["period"] == "daily"
    assert row["periods"] == 24
    assert row["first_period"] and row["last_period"]


def test_no_history_still_writes_a_row():
    """
    The rule that earns this function. A participant missing from
    participant_scores is one nobody can see is missing - and this is exactly
    what a registration that half-failed looks like.
    """
    row = score.score_participant("p1", "daily", [], [])
    assert row["sharpe"] is None
    assert row["reliable"] is False
    assert "at least 3" in row["unreliable_reason"]


def test_a_flat_account_writes_a_row_with_the_reason():
    row = score.score_participant("p1", "daily", rising(), [])
    assert row["sharpe"] is None
    assert "volatility" in row["unreliable_reason"]


def test_an_unexpected_error_costs_one_row_not_the_run():
    """A bug in the maths must not stop everyone else being scored."""
    row = score.score_participant("p1", "daily", "not a list", [])
    assert row["sharpe"] is None
    assert "scoring failed" in row["unreliable_reason"]


def test_a_row_always_has_the_primary_key_columns():
    """The upsert targets (participant_id, period); both must always be set."""
    for snapshots in ([], rising(), "garbage"):
        row = score.score_participant("p2", "hourly", snapshots, [])
        assert row["participant_id"] == "p2"
        assert row["period"] == "hourly"
        assert "computed_at" in row


# ---------------------------------------------------------------------------
# Non-finite guards
#
# A Sharpe ratio is a quotient. Postgres numeric has no Infinity and
# json.dumps writes bare `Infinity`, which is invalid JSON - so one strange
# value would fail the batch upsert and cost EVERY participant their score.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_values_are_not_stored(bad):
    assert score._storable(bad) is None


def test_ordinary_values_pass_through():
    assert score._storable(1.5) == 1.5
    assert score._storable(0) == 0
    assert score._storable(None) is None
    assert score._storable("2026-01-01") == "2026-01-01"


def test_a_non_finite_sharpe_is_reported_not_silently_dropped(monkeypatch):
    def fake(participant_id, period="daily", risk_free_rate=0.0,
             snapshots=None, flows=None):
        return {"sharpe": float("inf"), "periods": 30, "reliable": True,
                "mean_return": 0.1, "volatility": 0.0}

    monkeypatch.setattr(metrics, "participant_sharpe", fake)
    row = score.score_participant("p1", "daily", rising(), [])

    assert row["sharpe"] is None
    assert row["reliable"] is False
    assert "finite" in row["unreliable_reason"]


# ---------------------------------------------------------------------------
# A venue joining the portfolio is a FLOW, not a return
#
# The asymmetry that made this wrong: build_portfolio_series always booked a
# venue that DISAPPEARED as a withdrawal, but summed a venue that APPEARED
# straight into the total, where it read as profit.
#
# Measured on live data: one participant's portfolio went 26.09 -> 39.04 the
# day their Lighter credential was registered, scored as +49.6%, on a day
# their Coinbase perp actually lost money.
# ---------------------------------------------------------------------------

def test_a_venue_joining_is_not_scored_as_a_gain():
    snapshots = [
        snap(1, 100, account_type="spot"), snap(2, 100, account_type="spot"),
        snap(3, 100, account_type="spot"), snap(4, 100, account_type="spot"),
        # a second venue appears on day 3 carrying 50
        snap(3, 50, exchange="lighter", account_type="perp"),
        snap(4, 50, exchange="lighter", account_type="perp"),
    ]
    series, adjustments = metrics.build_portfolio_series(snapshots, "daily")

    assert dict(series)["2026-01-03"] == 150.0        # the total does include it
    assert adjustments["2026-01-03"] == 50.0          # but it is booked as an inflow

    # ...so the return for that day is 0%, not +50%
    returns = metrics.period_returns(series, adjustments)
    assert returns[1] == pytest.approx(0.0)


def test_the_opening_portfolio_is_not_booked_as_a_flow():
    """No return ends at the first period, so an adjustment there would only
    misreport the starting balance as a deposit."""
    _, adjustments = metrics.build_portfolio_series(
        [snap(1, 100), snap(2, 100), snap(3, 100)], "daily")
    assert "2026-01-01" not in adjustments


def test_a_venue_returning_after_being_dropped_is_booked_back_in():
    """
    It was booked OUT as a withdrawal when it went stale, so it has to be
    booked back in - otherwise its reappearance is free profit.
    """
    snapshots = [snap(1, 100, account_type="spot")]
    snapshots += [snap(d, 100, account_type="spot") for d in range(2, 12)]
    snapshots.append(snap(1, 40, exchange="lighter", account_type="perp"))
    snapshots.append(snap(11, 40, exchange="lighter", account_type="perp"))

    _, adjustments = metrics.build_portfolio_series(
        snapshots, "daily", max_stale_periods=3)

    assert any(v < 0 for v in adjustments.values()), "never booked out"
    assert any(v > 0 for v in adjustments.values()), "never booked back in"


def test_venue_first_seen_reports_the_earliest_period_per_venue():
    seen = metrics.venue_first_seen([
        snap(5, 10, account_type="spot"),
        snap(2, 10, account_type="spot"),
        snap(7, 10, exchange="lighter", account_type="perp"),
    ], "daily")
    assert seen[("coinbase", "spot")] == "2026-01-02"
    assert seen[("lighter", "perp")] == "2026-01-07"


# ---------------------------------------------------------------------------
# The mirror half: a transfer dated before its venue exists
# ---------------------------------------------------------------------------

def test_a_deposit_before_its_venue_appears_is_not_double_counted():
    """
    Money leaves Coinbase on Monday; the Lighter credential is registered
    Wednesday. Counting the deposit on Monday subtracts it from a portfolio
    that does not contain that venue yet - a large fabricated loss - and then
    the venue's opening balance subtracts it again on Wednesday.

    Live example: a +15 USDC Lighter deposit recorded two days early turned a
    +5.4% day into -56.6%.
    """
    flows = [flow(1, 15.0, exchange="lighter", account_type="perp")]
    since = {("lighter", "perp"): "2026-01-03"}

    assert metrics.flows_by_period(flows, "daily") == {"2026-01-01": 15.0}
    assert metrics.flows_by_period(flows, "daily", since) == {}


def test_a_deposit_after_its_venue_appears_still_counts():
    """The filter must not swallow genuine funding."""
    flows = [flow(5, 15.0, exchange="lighter", account_type="perp")]
    since = {("lighter", "perp"): "2026-01-03"}
    assert metrics.flows_by_period(flows, "daily", since) == {"2026-01-05": 15.0}


def test_a_deposit_in_the_joining_period_itself_counts():
    """Boundary: same period is not 'before'."""
    flows = [flow(3, 15.0, exchange="lighter", account_type="perp")]
    since = {("lighter", "perp"): "2026-01-03"}
    assert metrics.flows_by_period(flows, "daily", since) == {"2026-01-03": 15.0}


def test_flows_for_an_unknown_venue_are_kept():
    """Absence from the map means 'no snapshot seen', not 'ignore this'."""
    flows = [flow(1, 15.0, exchange="lighter", account_type="perp")]
    assert metrics.flows_by_period(flows, "daily", {}) == {"2026-01-01": 15.0}


def test_joins_and_drops_are_reported_separately():
    """
    Netting them would hide both: a venue leaving is money out of the
    competition, a venue joining is money in.
    """
    # Long enough for the drop to actually fire: lighter is last seen on day
    # 3, and max_stale_periods=3 means it is written off on day 7.
    snapshots = [snap(d, 100, account_type="spot") for d in range(1, 10)]
    snapshots += [snap(d, 50, exchange="lighter", account_type="perp")
                  for d in (2, 3)]
    r = metrics.participant_sharpe("p1", snapshots=snapshots, flows=[])
    assert r["joined_venue_usdc"] == 50.0, "venue joining was not booked in"
    assert r["dropped_venue_usdc"] == -50.0, "venue going stale was not booked out"


# ---------------------------------------------------------------------------
# The competition window
#
# Scoring is bounded at BOTH ends; fetching is not. The sync keeps recording
# after the close - a useful audit trail - but a leaderboard that keeps moving
# afterwards has no final result.
# ---------------------------------------------------------------------------

@pytest.fixture
def window(monkeypatch):
    """Set the competition window for one test."""
    # End-of-day, as a real competition close would be. Set it to 00:00 and
    # the final day's snapshots - which land at 12:00 - fall outside their own
    # last day, which is exactly the off-by-one this default avoids.
    def _set(start="2026-01-03T00:00:00Z", end="2026-01-07T23:59:59Z"):
        import venue_common
        monkeypatch.setattr(venue_common, "COMPETITION_START", start)
        monkeypatch.setattr(venue_common, "COMPETITION_END", end)
    return _set


def test_history_before_the_start_is_not_scored(window):
    """
    A participant trading long before the competition opened must not carry
    that history in. Their first in-window snapshot is their starting line.
    """
    window()
    rows = [snap(d, 100) for d in range(1, 8)]
    kept = metrics.clip_to_competition(rows)
    assert len(kept) == 5                      # days 3..7
    assert metrics._period_key(kept[0]["timestamp"], "daily") == "2026-01-03"


def test_activity_after_the_end_is_not_scored(window):
    """The property the end date exists for."""
    window()
    rows = [snap(d, 100) for d in range(1, 15)]
    kept = metrics.clip_to_competition(rows)
    assert metrics._period_key(kept[-1]["timestamp"], "daily") == "2026-01-07"


def test_the_final_ranking_stops_moving_after_the_close(window):
    """
    The whole point. Without an end date, day 57's market movement silently
    rewrites the standings of a contest that already finished, and every rank
    drifts for as long as the cron keeps running.
    """
    window()
    during = volatile(days=7)
    after = during + [snap(d, 999_999.0) for d in range(8, 20)]

    a = metrics.participant_sharpe("p1", snapshots=during, flows=[])
    b = metrics.participant_sharpe("p1", snapshots=after, flows=[])

    assert a["sharpe"] == b["sharpe"]
    assert a["last"] == b["last"] == "2026-01-07"


def test_an_unset_end_leaves_the_window_open(monkeypatch):
    """Right for development, wrong for a competition with a prize."""
    import venue_common
    monkeypatch.setattr(venue_common, "COMPETITION_START", "2026-01-01T00:00:00Z")
    monkeypatch.setattr(venue_common, "COMPETITION_END", None)

    rows = [snap(d, 100) for d in range(1, 30)]
    assert len(metrics.clip_to_competition(rows)) == 29


def test_deposits_outside_the_window_are_dropped_too(window):
    """
    Flows are clipped on the same boundary as snapshots. A deposit made after
    the close cannot change a finished competition's returns.
    """
    window()
    flows = [flow(1, 500.0), flow(5, 500.0), flow(12, 500.0)]
    kept = metrics.clip_to_competition(flows)
    assert len(kept) == 1
    assert metrics._period_key(kept[0]["timestamp"], "daily") == "2026-01-05"


def test_a_row_with_no_timestamp_is_dropped_not_kept(window):
    """It cannot be placed in the window, so it cannot be scored."""
    window()
    assert metrics.clip_to_competition([{"total_usdc": 1.0}]) == []


def test_the_boundaries_are_inclusive(window):
    """A snapshot exactly on the closing instant counts."""
    window(start="2026-01-03T12:00:00Z", end="2026-01-05T12:00:00Z")
    rows = [snap(3, 1), snap(4, 1), snap(5, 1)]      # all at 12:00
    assert len(metrics.clip_to_competition(rows)) == 3
