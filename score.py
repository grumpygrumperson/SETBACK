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

    python score.py                 # every participant, daily + hourly
    python score.py --period daily
"""

import logging
import os
import sys
from datetime import datetime, timezone
from math import isfinite

import metrics

logger = logging.getLogger(__name__)

# Which periods to compute. 'daily' is what the leaderboard ranks on; 'hourly'
# is stored alongside it as an early-competition signal, because `reliable`
# needs 20 periods and nobody has 20 DAYS for the first three weeks of a
# 90-day competition.
#
# Hourly is deliberately NOT the ranking. Annualising hourly returns by
# sqrt(8760) rewards an account that simply sits still, which is the opposite
# of what this competition is measuring.
PERIODS = ("daily", "hourly")

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

    snapshots_by = metrics.fetch_snapshots_for(ids)
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
