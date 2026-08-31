"""
Compute every participant's score and store it.

This is the module that makes the competition a competition. Before it
existed, metrics.py could compute a flow-adjusted Sharpe ratio but nothing
ever called it, and the only ranking available was latest_balances.total_usdc
- which ranks whoever DEPOSITED the most. On live data the two orderings came
out exactly inverted: the participant with 2,000x more money in the account
had the worse risk-adjusted return.

Separate from metrics.py, which owns the maths and stays pure and testable,
and separate from post_to_supabase.py, which owns fetching. This module only
moves numbers from one to the other.

Run standalone to rescore without syncing:

    python score.py                 # every participant, daily
    python score.py --period daily  # explicit; same thing
"""

import logging
import os
import sys
from datetime import datetime, timezone
from math import isfinite

import metrics

logger = logging.getLogger(__name__)

# Which periods to compute. Daily, and only daily.
#
# 'hourly' used to be stored beside it as an early-competition signal, on the
# grounds that `reliable` needs 20 periods and nobody has 20 DAYS in the first
# three weeks. It was never the ranking - annualising hourly returns by
# sqrt(8760) rewards an account that simply sits still, which is the opposite
# of what this competition measures.
#
# It is gone for two independent reasons, either sufficient on its own:
#
#   1. It contradicts the ranking in public. On live data one participant
#      scored -1.836 daily and +1.885 hourly - same account, same week,
#      opposite sign. Publishing both invites a disputed rank to be argued
#      with whichever number flatters the arguer.
#
#   2. It cannot survive the cron interval. An hourly axis needs an hourly
#      snapshot. At the 3-hour cadence this service actually ran at, two of
#      every three hourly periods were holes; and since
#      build_portfolio_series writes a venue off after 3 stale periods, one
#      additional missed run books every venue out as a withdrawal and back
#      in as a join - fabricating structural adjustments from nothing.
#
# Dropping it is also what allows scoring to read daily_balances rather than
# every snapshot ever written. See metrics._SNAPSHOT_SOURCES for the size of
# that difference.
PERIODS = ("daily",)

# Columns of participant_scores that come straight from metrics' result dict,
# mapped from its key to the column name where they differ.
_RESULT_COLUMNS = {
    "sharpe": "sharpe",
    "periods": "periods",
    "observed_periods": "observed_periods",
    "gap_periods": "gap_periods",
    "calendar_periods": "calendar_periods",
    "mean_return": "mean_return",
    "volatility": "volatility",
    "external_flow_usdc": "external_flow_usdc",
    # Both halves of the structural adjustment, stored separately because they
    # mean opposite things: a venue leaving is money out of the competition, a
    # venue joining is money in. Storing only the drop would show a Sharpe
    # that had been adjusted by an amount nobody could see - and the join is
    # the larger correction in practice, since registering a second venue
    # mid-competition is routine while losing one is not.
    "dropped_venue_usdc": "dropped_venue_usdc",
    "joined_venue_usdc": "joined_venue_usdc",
    "internal_transfers": "internal_transfers",
    "first": "first_period",
    "last": "last_period",
}


def _storable(value):
    """
    Make a float safe for a Postgres `numeric` column.

    A Sharpe ratio is a quotient, and quotients produce inf and nan. Postgres
    numeric has no Infinity, and json.dumps writes bare `Infinity`, which is
    not valid JSON - so an unguarded non-finite value fails the whole batch
    upsert and costs EVERY participant their score, not just the one whose
    maths went strange.

    The volatility guard in sharpe_from_returns should prevent this. This is
    here because "should" is not a property you want between a division and a
    database write.
    """
    if isinstance(value, float) and not isfinite(value):
        return None
    return value


def score_participant(participant_id: str, period: str,
                      snapshots: list[dict], flows: list[dict]) -> dict:
    """
    One participant's score row for one period. Never raises.

    ALWAYS returns a row, even when there is no score to give. That is the
    point rather than an accident: a participant absent from
    participant_scores is a participant nobody can see is missing, and there
    are three quite different ways to end up with no number -

      too little history   new, or a credential that broke early
      no volatility        a flat account; sharpe_from_returns says so
      a registration bug   a participants row whose credential write failed,
                           which is invisible everywhere else in the system

    All three produce a row with sharpe = NULL and unreliable_reason set, so
    the leaderboard shows the participant and says why they have no score.
    The scorer ends up being the detector for that last one at no extra cost.
    """
    row = {
        "participant_id": participant_id,
        "period": period,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "sharpe": None,
        "reliable": False,
        "unreliable_reason": None,
    }

    try:
        result = metrics.participant_sharpe(
            participant_id, period=period, snapshots=snapshots, flows=flows
        )
    except ValueError as e:
        # Not enough history to measure anything. Expected for anyone who
        # registered in the last few days - not an error, just an answer.
        row["unreliable_reason"] = str(e)
        return row
    except Exception as e:
        # Anything else is a real bug, but it must cost one participant their
        # score rather than the whole run's.
        logger.exception("Scoring %s (%s) failed", participant_id, period)
        row["unreliable_reason"] = f"scoring failed: {type(e).__name__}: {e}"
        return row

    for key, column in _RESULT_COLUMNS.items():
        row[column] = _storable(result.get(key))

    row["reliable"] = bool(result.get("reliable"))

    # `reason` is set by sharpe_from_returns when volatility is effectively
    # zero, which is a real answer about a flat account rather than a failure.
    if result.get("sharpe") is None:
        row["unreliable_reason"] = result.get("reason") or "no score computed"
    elif row["sharpe"] is None:
        # _storable rejected it - a non-finite quotient.
        row["unreliable_reason"] = "computed value was not a finite number"
        row["reliable"] = False

    return row


def _drop_retired_periods() -> None:
    """
    Delete score rows for periods this module no longer computes.

    Without this the hourly rows written before that period was dropped sit
    in participant_scores indefinitely, carrying a computed_at that makes
    them look current. The leaderboard view filters on period = 'daily' and
    is unaffected - but anyone querying participant_scores directly, which is
    the natural thing to do when investigating a disputed rank, gets a
    number nobody stands behind any more.

    Compares against the module-level PERIODS rather than the caller's
    argument on purpose: PERIODS declares what SHOULD exist, so
    `--period daily` still clears retired rows instead of depending on which
    subset happened to be requested.

    The select is unpaginated because the table holds exactly one row per
    participant per period - a few hundred rows at this competition's size.
    """
    existing = (metrics.supabase.table("participant_scores")
                .select("period").execute().data) or []

    retired = sorted({row["period"] for row in existing} - set(PERIODS))
    if not retired:
        return

    metrics.supabase.table("participant_scores").delete().in_("period", retired).execute()
    logger.info("Removed score row(s) for retired period(s): %s",
                ", ".join(retired))


def write_all_scores(periods: tuple = PERIODS) -> dict:
    """
    Score every participant and upsert the results.

    Reads all history in a handful of batched queries rather than two per
    participant - scoring 200 people one at a time is 400 paged scans before
    any maths happens, the same N+1 the sync itself used to have.

    Returns a summary for the caller to log.
    """
    participants = (metrics.supabase.table("participants")
                    .select("id").execute().data) or []
    ids = [p["id"] for p in participants]

    if not ids:
        logger.warning("No participants to score")
        return {"participants": 0, "rows": 0, "scored": 0, "unscored": 0}

    # A daily axis can be built from daily rows; anything finer cannot. Ask
    # for what the requested periods actually require rather than assuming,
    # so `--period hourly` stays correct if it is ever wanted again.
    resolution = "daily" if set(periods) <= {"daily"} else "full"

    snapshots_by = metrics.fetch_snapshots_for(ids, resolution=resolution)
    flows_by = metrics.fetch_cash_flows_for(ids)

    rows = []
    for participant_id in ids:
        snapshots = snapshots_by.get(participant_id, [])
        flows = flows_by.get(participant_id, [])
        for period in periods:
            rows.append(score_participant(
                participant_id, period, snapshots, flows))

    # One upsert for everyone. The primary key is (participant_id, period), so
    # this replaces the current row rather than accumulating history - the
    # score is recomputed from complete stored history every run, so a time
    # series is derivable from balance_snapshots whenever it is wanted.
    metrics.supabase.table("participant_scores").upsert(
        rows, on_conflict="participant_id,period"
    ).execute()

    _drop_retired_periods()

    scored = sum(1 for r in rows if r["sharpe"] is not None)
    summary = {
        "participants": len(ids),
        "rows": len(rows),
        "scored": scored,
        "unscored": len(rows) - scored,
    }

    logger.info("Scored %d participant(s): %d row(s), %d with a score, "
                "%d without", summary["participants"], summary["rows"],
                summary["scored"], summary["unscored"])
    return summary


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    periods = PERIODS
    if "--period" in sys.argv:
        periods = (sys.argv[sys.argv.index("--period") + 1],)

    write_all_scores(periods)

    board = (metrics.supabase.table("leaderboard")
             .select("rank,display_name,sharpe,reliable,periods,total_usdc,"
                     "unreliable_reason")
             .order("rank").execute().data) or []

    print(f"\n{'rank':>4}  {'name':10} {'sharpe':>8}  {'rel':>4} {'per':>4}  "
          f"{'equity':>14}")
    for r in board:
        sharpe = f"{float(r['sharpe']):.3f}" if r["sharpe"] is not None else "n/a"
        note = "" if r["sharpe"] is not None else f"   {r['unreliable_reason'] or ''}"[:70]
        print(f"{r['rank']:>4}  {r['display_name']:10} {sharpe:>8}  "
              f"{str(r['reliable'])[:3]:>4} {r['periods'] or 0:>4}  "
              f"{float(r['total_usdc'] or 0):>14.2f}{note}")
