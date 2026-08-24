"""
Performance metrics computed from balance_snapshots.

Kept separate from the fetch/post pipeline so the maths can be tested without
touching an exchange, and so metrics can be recomputed over stored history
without re-fetching anything.
"""

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logger = logging.getLogger(__name__)


def _require_env(*names: str) -> None:
    """
    Name what's missing, rather than surfacing a SupabaseException several
    frames inside the library.

    Deliberately duplicated from post_to_supabase rather than imported:
    importing that module would run its own env check (including FERNET_KEY,
    which metrics don't need) and build a second Supabase client as a side
    effect. Ten lines is cheaper than that coupling.
    """
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Set them in your .env, or in the service's Variables on your host."
        )


# Metrics only read, so no FERNET_KEY here - nothing is decrypted.
_require_env("SUPABASE_URL", "SUPABASE_KEY")

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

# Crypto trades continuously, so a year is 365 days rather than 252 sessions.
PERIODS_PER_YEAR = {
    "daily": 365,
    "hourly": 24 * 365,
}

# How a period is labelled, and how long one lasts. Kept beside
# PERIODS_PER_YEAR so adding a period means editing one region, not three.
_PERIOD_FORMATS = {
    "daily": "%Y-%m-%d",
    "hourly": "%Y-%m-%dT%H",
}
_PERIOD_STEPS = {
    "daily": timedelta(days=1),
    "hourly": timedelta(hours=1),
}

# Ceiling on how long one participant's history may span, as a multiple of a
# year. Exists to turn a bad timestamp into a readable error rather than a
# silently enormous series: a snapshot stored in SECONDS rather than
# milliseconds parses as 1970.
#
# Expressed in YEARS rather than as a flat row count so it catches the daily
# case too - 1970 to now is only ~20,500 days, which any fixed cap large
# enough for hourly data would wave straight through.
_MAX_YEARS = 5


def fetch_snapshots(participant_id: str) -> list[dict]:
    """
    All of a participant's balance snapshots, every venue, oldest first.
    """
    response = (
        supabase.table("balance_snapshots")
        .select("account_type,timestamp,total_usdc")
        .eq("participant_id", participant_id)
        .order("timestamp")
        .execute()
    )
    return response.data or []


def fetch_cash_flows(participant_id: str) -> list[dict]:
    """
    All recorded transfers for a participant, every venue, oldest first.
    """
    response = (
        supabase.table("cash_flows")
        .select("account_type,timestamp,usdc_value,direction,currency,amount")
        .eq("participant_id", participant_id)
        .order("timestamp")
        .execute()
    )
    return response.data or []


def mark_internal_transfers(flows: list[dict], window_ms: int = 3_600_000,
                            tolerance: float = 0.02) -> list[dict]:
    """
    Flag transfers that just moved money between a participant's OWN venues.

    Spot and perps are one competition, so a participant topping up perp
    collateral from spot has changed nothing about their standing - but the
    exchange reports it twice: a withdrawal from spot and a deposit to perp.
    Counting those as real flows would corrupt the returns of exactly the
    participants who are most active.

    A pair is treated as internal when it is: opposite directions, different
    venues, close in time (`window_ms`), and close in value (`tolerance`,
    fractional - transfer fees mean the two legs rarely match exactly).

    Returns the same list with an 'is_internal' key set on every flow.
    """
    for flow in flows:
        flow["is_internal"] = False

    for i, a in enumerate(flows):
        if a["is_internal"]:
            continue
        for b in flows[i + 1:]:
            if b["is_internal"]:
                continue
            if b["timestamp"] - a["timestamp"] > window_ms:
                break                       # sorted, so nothing later can match
            if a["account_type"] == b["account_type"]:
                continue
            if a["direction"] == b["direction"]:
                continue

            size_a, size_b = abs(float(a["usdc_value"])), abs(float(b["usdc_value"]))
            if size_a == 0:
                continue
            if abs(size_a - size_b) / size_a <= tolerance:
                a["is_internal"] = b["is_internal"] = True
                break

    return flows


def flows_by_period(flows: list[dict], period: str = "daily") -> dict[str, float]:
    """
    Net EXTERNAL flow per period, signed (deposits positive).

    Internal transfers are excluded - they net to roughly zero anyway, but
    dropping them explicitly means a mismatched pair can't leak a spurious
    flow into someone's returns.
    """
    totals: dict[str, float] = {}
    for flow in flows:
        if flow.get("is_internal"):
            continue
        key = _period_key(flow["timestamp"], period)
        totals[key] = totals.get(key, 0.0) + float(flow["usdc_value"] or 0.0)
    return totals


def _period_key(timestamp_ms: int, period: str) -> str:
    """
    Bucket an exchange millisecond timestamp into a period label (UTC).
    """
    if period not in _PERIOD_FORMATS:
        raise ValueError(
            f"Unknown period '{period}' - expected one of {sorted(_PERIOD_FORMATS)}"
        )
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.strftime(_PERIOD_FORMATS[period])


def _period_range(first_key: str, last_key: str, period: str) -> list[str]:
    """
    Every period label from `first_key` to `last_key` inclusive - including the
    ones with no data.

    This is the fix for a bug that quietly inflated Sharpe ratios. The series
    used to be built from the periods that HAPPENED to have snapshots, so a
    sync outage was invisible: five missing days collapsed into a single
    adjacent pair, and the whole five-day move was then treated as one daily
    return and annualised by sqrt(365). The participants with the least
    reliable sync got the most distorted scores.

    Enumerating the calendar instead means a gap stays a gap, and callers can
    decide what to do about it rather than never learning it was there.
    """
    if period not in _PERIOD_STEPS:
        raise ValueError(
            f"Unknown period '{period}' - expected one of {sorted(_PERIOD_STEPS)}"
        )

    fmt, step = _PERIOD_FORMATS[period], _PERIOD_STEPS[period]
    current = datetime.strptime(first_key, fmt).replace(tzinfo=timezone.utc)
    end = datetime.strptime(last_key, fmt).replace(tzinfo=timezone.utc)

    limit = _MAX_YEARS * PERIODS_PER_YEAR[period]
    keys = []
    while current <= end:
        keys.append(current.strftime(fmt))
        if len(keys) > limit:
            raise ValueError(
                f"Refusing to build a {period} axis spanning more than "
                f"{_MAX_YEARS} years ({first_key} to {last_key}). A snapshot "
                f"timestamp is almost certainly wrong - "
                f"balance_snapshots.timestamp is in MILLISECONDS, and a value "
                f"stored in seconds parses as 1970."
            )
        current += step

    return keys


def build_portfolio_series(snapshots: list[dict], period: str = "daily",
                           max_stale_periods: int = 3) -> tuple[list[tuple[str, float | None]], dict[str, float]]:
    """
    Collapse per-venue snapshots into one portfolio value per period, over a
    CONTIGUOUS calendar axis.

    Four things this has to get right:

    1. Spot and perp are ONE competition but are written seconds apart, under
       different timestamps. They're bucketed into the same period and summed,
       taking the LAST snapshot of each venue within the period.

    2. A venue missing from one period - a failed fetch - is carried forward
       at its last known value. Summing only what's present would read as a
       sudden crash and manufacture a huge negative return.

    3. But a venue that stops reporting for good (a deactivated key) must NOT
       be carried forever, or the participant keeps credit for money they no
       longer have. After `max_stale_periods` it's dropped.

       Dropping is itself a trap: the portfolio value falls, which looks like
       a catastrophic loss. So each dropped venue is also reported as a
       negative structural adjustment, to be applied like a withdrawal - the
       money left the competition, it wasn't lost trading.

    4. A period where NO venue reported is a hole in the record, not a
       datapoint. It's emitted as None rather than skipped, because skipping
       it makes the surrounding periods look adjacent: five missing days would
       collapse into one pair, and the entire five-day move would be scored as
       a single daily return. period_returns() drops any return touching a
       None instead of inventing one.

       Note the difference from case 2: there, at least one venue reported, so
       the total is knowable and the missing venue is carried. Here nothing
       reported and the total is genuinely unknown.

    Staleness in case 3 is now counted in CALENDAR periods too - previously it
    counted only periods that had data, so a venue could never be detected as
    stale during a total outage.

    Returns (series, adjustments) where series is
    [(period_label, total_usdc_or_None), ...] over every period from the first
    snapshot to the last, and adjustments maps period_label -> signed value
    removed.
    """
    # last snapshot per (period, venue)
    by_period: dict[str, dict[str, float]] = {}
    for snap in snapshots:
        value = snap.get("total_usdc")
        if value is None:
            continue
        key = _period_key(snap["timestamp"], period)
        by_period.setdefault(key, {})[snap["account_type"]] = float(value)

    if not by_period:
        return [], {}

    observed = sorted(by_period)
    keys = _period_range(observed[0], observed[-1], period)

    series: list[tuple[str, float | None]] = []
    adjustments: dict[str, float] = {}

    carried: dict[str, float] = {}          # venue -> last known value
    last_seen: dict[str, int] = {}          # venue -> calendar index last seen

    for index, key in enumerate(keys):
        present = by_period.get(key)

        if present:
            for venue, value in present.items():
                carried[venue] = value
                last_seen[venue] = index

        stale = [v for v in carried if index - last_seen[v] > max_stale_periods]
        for venue in stale:
            # Treated as a withdrawal, not a loss
            adjustments[key] = adjustments.get(key, 0.0) - carried.pop(venue)
            last_seen.pop(venue, None)

        # None when nothing reported: the total is unknown, not zero and not
        # unchanged. See point 4 above.
        series.append((key, sum(carried.values()) if present else None))

    gaps = sum(1 for _, value in series if value is None)
    if gaps:
        logger.info(
            "%d of %d %s periods have no snapshot from any venue - returns "
            "spanning them are excluded", gaps, len(series), period
        )

    return series, adjustments


def period_returns(series: list[tuple[str, float | None]],
                   flows: dict[str, float] = None) -> list[float]:
    """
    Simple returns between consecutive periods, adjusted for external flows.

    This is what separates trading from funding. A participant whose account
    goes 1000 -> 2000 purely by depositing 1000 has earned nothing:

        raw:      2000 / 1000 - 1              = +100%
        adjusted: (2000 - 1000) / 1000 - 1     =    0%

    The flow is subtracted from the ENDING value, which assumes money arrived
    at the end of the period. With daily periods and 15-minute snapshots the
    error from that assumption is small; a mid-period deposit that was then
    traded gets slightly misattributed, which is the standard trade-off in
    time-weighted return.

    A None endpoint means that period has no snapshot from any venue, so no
    return is produced for either pair touching it. Every return in the result
    therefore spans exactly ONE period, which is what makes the sqrt(N)
    annualisation in sharpe_from_returns valid.

    Dropping is deliberate rather than computing the multi-period return and
    scaling it: a return over an unknown number of periods can't be annualised
    without assuming how the move was distributed across them, and that
    assumption is exactly what the missing data can't support.
    """
    flows = flows or {}
    returns = []

    for (_, start), (end_key, end) in zip(series, series[1:]):
        if start is None or end is None:
            continue                        # gap in the record; see docstring
        if start <= 0:
            continue                        # undefined; skip rather than divide by zero
        net_flow = flows.get(end_key, 0.0)
        returns.append((end - net_flow) / start - 1)

    return returns


def sharpe_from_returns(returns: list[float], risk_free_rate: float = 0.0,
                        periods_per_year: int = 365) -> dict:
    """
    Annualised Sharpe ratio from a series of period returns.

    Pure function - no database, no network - so it can be tested directly.

    `risk_free_rate` is the ANNUAL rate (0.04 for 4%), converted to a
    per-period rate internally. Defaults to 0, the usual convention for a
    competition ranking traders against each other rather than against cash.

    Returns the ratio alongside the inputs that produced it: a Sharpe figure
    without its sample size is not interpretable.
    """
    if len(returns) < 2:
        raise ValueError(
            f"Need at least 2 returns to measure volatility, got {len(returns)}"
        )

    rf_per_period = (1 + risk_free_rate) ** (1 / periods_per_year) - 1

    mean_return = mean(returns)
    volatility = stdev(returns)             # sample stdev (n-1)

    # Not `volatility == 0`: a perfectly steady series (a flat account, or
    # constant compounding) lands at ~1e-16 from floating point rather than
    # exactly zero, which would divide through to a nonsense Sharpe of 1e15.
    # Compare against the size of the returns themselves.
    if volatility <= max(1e-15, abs(mean_return) * 1e-9):
        return {
            "sharpe": None,
            "reason": "no measurable volatility - returns are effectively constant",
            "periods": len(returns),
            "mean_return": mean_return,
            "volatility": volatility,
        }

    return {
        "sharpe": ((mean_return - rf_per_period) / volatility) * math.sqrt(periods_per_year),
        "periods": len(returns),
        "mean_return": mean_return,
        "volatility": volatility,
        "annualisation": f"sqrt({periods_per_year})",
    }


def sharpe_ratio(values: list[float], risk_free_rate: float = 0.0,
                 periods_per_year: int = 365) -> dict:
    """
    Sharpe ratio from raw portfolio values, with NO adjustment for deposits
    or withdrawals.

    Use participant_sharpe() for anything that ranks people - it subtracts
    external flows first. This exists for testing the maths on a clean series.
    """
    if len(values) < 3:
        raise ValueError(
            f"Need at least 3 portfolio values to compute a Sharpe ratio, got "
            f"{len(values)} - that's only {max(len(values) - 1, 0)} return(s)"
        )

    series = [(str(i), v) for i, v in enumerate(values)]
    return sharpe_from_returns(period_returns(series), risk_free_rate, periods_per_year)


def participant_sharpe(participant_id: str, period: str = "daily",
                       risk_free_rate: float = 0.0) -> dict:
    """
    Flow-adjusted Sharpe ratio for one participant, across all their venues.

    Three corrections happen here, and all three matter:

      1. Spot and perp snapshots are summed into one portfolio per period -
         they're one competition, written seconds apart.
      2. Transfers between the participant's own venues are netted out; they
         move money without changing the participant's standing.
      3. Remaining external deposits and withdrawals are subtracted from the
         returns, so funding an account doesn't read as trading skill.

    A venue that stops reporting is dropped and treated as a withdrawal, on
    the same footing as an external transfer.
    """
    snapshots = fetch_snapshots(participant_id)
    series, adjustments = build_portfolio_series(snapshots, period)

    flows = mark_internal_transfers(fetch_cash_flows(participant_id))
    external = flows_by_period(flows, period)

    # Count periods that actually have data. len(series) is now the calendar
    # span, which a single snapshot on either side of a long outage would
    # inflate past this check while yielding no usable returns at all.
    observed = sum(1 for _, value in series if value is not None)
    if observed < 3:
        raise ValueError(
            f"Need at least 3 {period} portfolio values to compute a Sharpe "
            f"ratio, got {observed} across {len(series)} {period} period(s)"
        )

    # Dropped venues behave exactly like withdrawals, so they're merged into
    # the same per-period flow figure rather than handled separately.
    combined = dict(external)
    for key, value in adjustments.items():
        combined[key] = combined.get(key, 0.0) + value

    returns = period_returns(series, combined)
    result = sharpe_from_returns(returns, risk_free_rate, PERIODS_PER_YEAR[period])

    result["participant_id"] = participant_id
    result["period"] = period
    result["first"], result["last"] = series[0][0], series[-1][0]
    result["external_flow_usdc"] = sum(external.values())
    result["internal_transfers"] = sum(1 for f in flows if f.get("is_internal"))
    result["dropped_venue_usdc"] = sum(adjustments.values())

    # Reported, not hidden: a participant scored on 8 of 30 days is a data
    # quality problem, and the number is the only way a reader can tell that
    # from a participant who simply joined late.
    result["calendar_periods"] = len(series)
    result["observed_periods"] = observed
    result["gap_periods"] = len(series) - observed

    # A Sharpe ratio over a handful of days is noise, not signal. Say so in
    # the result rather than leaving the caller to know it. Gaps count against
    # it too - `periods` is now the number of single-period returns that
    # survived, so an outage lowers this rather than silently compressing.
    result["reliable"] = result["periods"] >= 20

    return result


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    for p in supabase.table("participants").select("id,display_name").execute().data:
        try:
            r = participant_sharpe(p["id"])
            # sharpe is None whenever the volatility guard fires - formatting
            # that with :.3f raises TypeError, so branch rather than assume.
            score = f"{r['sharpe']:.3f}" if r["sharpe"] is not None else f"n/a ({r['reason']})"
            gaps = f", {r['gap_periods']} gap period(s)" if r["gap_periods"] else ""
            print(f"{p['display_name']:8} sharpe={score}  "
                  f"({r['periods']} daily returns, {r['first']} to {r['last']}{gaps})")
        except ValueError as e:
            print(f"{p['display_name']:8} not enough history: {e}")
