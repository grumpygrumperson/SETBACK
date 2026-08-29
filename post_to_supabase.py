import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from supabase import create_client
from dotenv import load_dotenv

import venues

load_dotenv()

logger = logging.getLogger(__name__)


def _require_env(*names: str) -> None:
    """
    Fail with a readable message naming what's missing, rather than letting a
    library raise several frames deep. On a scheduled runner the environment
    is the most common thing to get wrong, and the default traceback doesn't
    say which variable was absent.
    """
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing) +
            ". Set them in your .env locally, or in the service's Variables "
            "on your host. FERNET_KEY must be identical to the key that "
            "encrypted the stored credentials, or nothing will decrypt."
        )


_require_env("SUPABASE_URL", "SUPABASE_KEY", "FERNET_KEY")

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

# Credential decryption lives in coinbase.build_exchange - the Fernet cipher
# is built there so only one place in the codebase touches the key.


def get_active_participants() -> list[dict]:
    """
    Fetches all participants from the Supabase database.
    """
    return supabase.table("participants").select("*").execute().data


def get_api_keys(participant_id) -> list[dict]:
    """
    Fetch one participant's active credential rows - one per venue, so a
    participant trading both spot and perps comes back as two rows, each
    carrying its own passphrase and portfolio UUID.

    The sync itself uses get_all_api_keys(); this stays for ad hoc use on a
    single participant.
    """
    response = (
        supabase.table("participant_api_keys")
        .select("*")
        .eq("participant_id", participant_id)
        .eq("is_active", True)
        .execute()
    )
    return response.data or []


def get_all_api_keys() -> dict[str, list[dict]]:
    """
    Every active credential in the competition, grouped by participant_id.

    One request instead of one per participant. The per-participant query was
    an N+1: at 200 participants the sync opened 200 round trips to Supabase
    before touching a single exchange, and each costs ~0.2s of pure latency
    whatever the row count. Grouping in memory is free by comparison, and the
    whole table is a few hundred small rows.

    Note this reads credentials for participants the loop may skip. That is
    fine - they are already encrypted, and get_active_participants() decides
    who is processed.
    """
    # Ordered, and not merely for tidiness. _plan_credential_work() picks the
    # FIRST credential of an account-wide venue to collect that participant's
    # transfers; without an ORDER BY, PostgREST returns rows in Postgres'
    # physical order, which shifts whenever a row is updated - so the winner
    # could change between runs. See get_last_flow_timestamp() for why that
    # used to matter and no longer does.
    response = (
        supabase.table("participant_api_keys")
        .select("*")
        .eq("is_active", True)
        .order("exchange")
        .order("account_type")
        .order("id")
        .execute()
    )

    by_participant: dict[str, list[dict]] = {}
    for row in response.data or []:
        by_participant.setdefault(row.get("participant_id"), []).append(row)
    return by_participant


def get_last_synced_timestamp(participant_id, exchange: str, account_type: str) -> int:
    """
    Return the millisecond timestamp to resume this participant's sync from,
    i.e. one past their most recently stored order for this venue.

    Neither `exchange` nor `account_type` is optional, and leaving either out
    causes the same silent data loss in a different direction:

      no account_type   a perp sync resumes from the last SPOT trade
      no exchange       a Coinbase perp sync resumes from the last LIGHTER
                        trade, because both venues store account_type='perp'

    Either way the resume point lands ahead of orders that were never
    fetched, and resume points only move forward - so those orders are lost
    for good rather than picked up on the next run.

    Returns None when there's nothing stored yet, which lets closed_orders()
    fall back to its own start-of-competition default.
    """
    response = (
        supabase.table("trade_metrics")
        .select("timestamp")
        .eq("participant_id", participant_id)
        .eq("exchange", exchange)
        .eq("account_type", account_type)
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    if rows and rows[0].get("timestamp") is not None:
        # +1ms so the boundary order isn't refetched on every run
        return int(rows[0]["timestamp"]) + 1
    return None


def log_fetch_error(participant_id, error: Exception, venue: str = None) -> None:
    """
    Record a per-credential failure so it's visible after an unattended run -
    a participant whose keys expire mid-competition would otherwise just stop
    appearing with nothing but stdout to say why.

    `venue` is a human label like 'coinbase/perp'. With two exchanges now
    reporting account_type='perp', an error tagged only 'perp' wouldn't say
    which one broke.
    """
    label = f"{participant_id} ({venue})" if venue else str(participant_id)
    logger.error("Failed to sync %s: %s", label, error)
    try:
        supabase.table("fetch_errors").insert({
            "participant_id": participant_id,
            "error_message": f"[{venue or 'unknown'}] {error}"[:500],
        }).execute()
    except Exception as e:
        # Never let error logging take down the run for everyone else
        logger.error("Could not record fetch error for %s: %s", label, e)


def sync_orders(participant_id, credential: dict, exchange, venue) -> int:
    """
    Upsert one credential's closed orders into trade_metrics. Returns the
    number of rows Supabase accepted.

    `venue` is the adapter module for this credential's exchange - coinbase
    or lighter. Both expose closed_orders() with the same signature, so
    nothing here branches on which venue it is.
    """
    exchange_id = credential.get("exchange")
    account_type = credential.get("account_type")
    portfolio_uuid = credential.get("portfolio_uuid")

    since = get_last_synced_timestamp(participant_id, exchange_id, account_type)
    orders = venue.closed_orders(exchange, since=since, portfolio_uuid=portfolio_uuid)

    if not orders:
        return 0

    for order in orders:
        order["participant_id"] = participant_id
        # The CREDENTIAL is authoritative, not anything the order says about
        # itself. Orders are fetched with a venue-scoped key, so they can only
        # have come from that venue - whereas Coinbase labels INTX perpetuals
        # 'FUTURE' with no contract_expiry_type, which reads as a dated
        # future. Storing a row under different venue columns than the ones
        # get_last_synced_timestamp() queries makes the resume point invisible
        # and re-fetches the whole history every run.
        order["exchange"] = exchange_id
        order["account_type"] = account_type

    response = supabase.table("trade_metrics").upsert(
        orders,
        on_conflict="participant_id,exchange,account_type,order_id",
        ignore_duplicates=False,
    ).execute()

    # response.data is None on some error paths - don't let the count raise
    # and mask the real failure.
    return len(response.data or [])


def sync_balance_snapshot(participant_id, credential: dict, exchange, venue) -> None:
    """
    Record one point on this credential's equity curve.

    Every venue's valuation returns a 'total_usdc' headline figure, so they
    all store identically; whatever else each returns is kept in `detail`
    rather than discarded, since it can't be backfilled later.
    """
    exchange_id = credential.get("exchange")
    account_type = credential.get("account_type")
    portfolio_uuid = credential.get("portfolio_uuid")

    snapshot = venue.get_account_totals_usdc(
        exchange,
        account_type=account_type,
        portfolio_uuid=portfolio_uuid,
    )

    detail = {k: v for k, v in snapshot.items()
              if k not in ("timestamp", "datetime", "account_type", "total_usdc")}

    # upsert, not insert: the table is unique on
    # (participant_id, exchange, account_type, timestamp), so a retry landing
    # on the same millisecond would otherwise raise instead of being a no-op.
    supabase.table("balance_snapshots").upsert({
        "participant_id": participant_id,
        "exchange": exchange_id,
        "account_type": account_type,
        "timestamp": snapshot.get("timestamp"),
        "total_usdc": snapshot.get("total_usdc"),
        "detail": detail,
    }, on_conflict="participant_id,exchange,account_type,timestamp").execute()


def sync_cash_flows(participant_id, credential: dict, exchange, venue) -> int:
    """
    Record external deposits and withdrawals for one credential.

    Returns are meaningless without these: money arriving in an account looks
    identical to money earned in it. Stored raw and unjudged - deciding which
    transfers are internal happens in the metrics layer, where a participant's
    venues can be compared against each other.
    """
    exchange_id = credential.get("exchange")
    account_type = credential.get("account_type")
    portfolio_uuid = credential.get("portfolio_uuid")

    # An account-wide history resumes from the newest transfer stored for this
    # participant on this EXCHANGE, whichever credential happened to record
    # it. See get_last_flow_timestamp().
    account_wide = getattr(venue, "CASH_FLOWS_ARE_ACCOUNT_WIDE", False)
    since = get_last_flow_timestamp(
        participant_id, exchange_id, None if account_wide else account_type
    )

    flows = venue.get_cash_flows(exchange, since=since,
                                 portfolio_uuid=portfolio_uuid)

    if not flows:
        return 0

    for flow in flows:
        flow["participant_id"] = participant_id
        flow["exchange"] = exchange_id
        flow["account_type"] = account_type

    response = supabase.table("cash_flows").upsert(
        flows,
        on_conflict="participant_id,exchange,account_type,transfer_id",
        ignore_duplicates=False,
    ).execute()

    return len(response.data or [])


def get_last_flow_timestamp(participant_id, exchange: str,
                            account_type: str = None) -> int:
    """
    Resume point for a transfer history, mirroring get_last_synced_timestamp
    for orders.

    `account_type=None` means "anywhere on this exchange", and that is the
    right query for a venue whose transfer history covers the whole account.

    Scoping it to one account_type there is a trap. Only ONE credential per
    (participant, venue) collects account-wide transfers, and which one that
    is depends on the order credentials come back in. If a run stored a
    participant's Coinbase deposits under 'spot' and a later run collected
    them under 'perp' - because they deactivated a key, registered another,
    or the rows simply came back in a different order - the 'perp' lookup
    would find no history, resume from the start of the competition, and
    write every transfer a SECOND time. The unique key includes account_type,
    so nothing would reject it, and flows_by_period() would then subtract
    every deposit twice.

    Ignoring account_type makes the resume point survive the winner changing.
    Per-account venues like Lighter still pass their account_type, because
    there each credential genuinely has its own separate history.
    """
    query = (
        supabase.table("cash_flows")
        .select("timestamp")
        .eq("participant_id", participant_id)
        .eq("exchange", exchange)
    )
    if account_type is not None:
        query = query.eq("account_type", account_type)

    response = query.order("timestamp", desc=True).limit(1).execute()

    rows = response.data or []
    if rows and rows[0].get("timestamp") is not None:
        return int(rows[0]["timestamp"]) + 1
    return None


def exit_code_for(attempted: int, failed: int, threshold: float = 1.0) -> int:
    """
    Decide whether a run counts as a failure, given how many credentials were
    tried and how many produced nothing.

    The distinction this draws is between a PARTICIPANT's problem and an
    OPERATOR's problem:

      one credential fails    that participant revoked their key. It's already
                              in fetch_errors and is fixed by emailing them.
                              Failing the run for it means that once anyone
                              abandons the competition, every run is red
                              forever - and a permanently red job is one
                              nobody looks at, which costs you the alerting
                              the exit code existed to provide.

      every credential fails  wrong FERNET_KEY, Supabase unreachable, the
                              exchange down. Nothing will fix itself and the
                              run should be loud.

    `threshold` is the failure FRACTION at or above which the run fails:

        1.0   (default) only a total wipeout is an error
        0.5   half the field broken is an error
        0.0   any failure at all is an error

    A run with zero failures is never an error, whatever the threshold.
    A run that attempted nothing always is - an empty participants table
    means registration is broken, and reporting success for having done
    nothing is how that goes unnoticed.
    """
    if attempted == 0:
        return 1
    if failed == 0:
        return 0
    return 1 if (failed / attempted) >= threshold else 0


def _failure_threshold() -> float:
    """
    Read SYNC_FAILURE_THRESHOLD, falling back to 1.0 on anything unusable.

    A malformed value must not take down the run: the sync working matters
    more than the alerting policy being exactly right.
    """
    raw = os.getenv("SYNC_FAILURE_THRESHOLD")
    if not raw:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("SYNC_FAILURE_THRESHOLD=%r is not a number - using 1.0", raw)
        return 1.0
    if not 0.0 <= value <= 1.0:
        logger.warning("SYNC_FAILURE_THRESHOLD=%s is outside 0.0-1.0 - using 1.0", value)
        return 1.0
    return value


def _plan_credential_work(participants: list[dict],
                          credentials_by_participant: dict) -> tuple[list[dict], int]:
    """
    Decide, before anything runs, exactly what each credential should do.

    Two things are settled here rather than inside the sync so that the work
    items are independent and can run in any order - or at the same time:

      * which venue adapter handles each credential, and
      * which ONE credential per (participant, venue) collects cash flows,
        for venues whose transfer history covers the whole account.

    That second decision cannot be made inside the workers. It is a
    per-participant "has anyone done this yet", and two threads reaching it
    together would both answer no - reintroducing exactly the duplicate
    deposits the check exists to prevent. Settling it up front makes it
    deterministic and needs no shared state at all.

    Returns (work_items, skipped) where skipped counts active rows with no
    key stored.
    """
    work: list[dict] = []
    skipped = 0

    for participant in participants:
        participant_id = participant.get("id")
        rows = credentials_by_participant.get(participant_id, [])
        flow_collector = _flow_collectors(rows)

        for credential in rows:
            exchange_id = credential.get("exchange") or "coinbase"
            account_type = credential.get("account_type")
            label = f"{exchange_id}/{account_type}"

            # api_secret is deliberately NOT required: Lighter authenticates
            # with a single value in api_key, so demanding a pair here would
            # skip every Lighter credential in silence.
            if not credential.get("api_key"):
                # A registration problem rather than a sync failure, but not
                # silent either - an active row with no key never syncs.
                skipped += 1
                logger.warning("%s (%s): active credential row has no key stored",
                               participant_id, label)
                continue

            try:
                venue = venues.get(exchange_id)
            except venues.UnknownVenue as e:
                # Registered against an exchange with no adapter. Counted as a
                # failure rather than skipped: someone signed up expecting to
                # be scored, and silently omitting them from the leaderboard
                # is the worst available outcome - worse still on a
                # cross-exchange strategy, where the remaining venue reads as
                # a naked position rather than one leg of a hedge.
                log_fetch_error(participant_id, e, label)
                work.append({"participant_id": participant_id, "label": label,
                             "unknown_venue": True})
                continue

            # Some venues report transfers for the whole ACCOUNT rather than
            # for the credential's own portfolio. Coinbase is one: its v2
            # transactions endpoint returns the same history whichever
            # portfolio's key asks, so a participant holding both a spot and
            # a perp credential would have every deposit stored twice - once
            # under each account_type - and the metrics layer subtracts both.
            # A $1,000 deposit then reads as $2,000 of funding, and the
            # participant's return is understated for the rest of the
            # competition.
            #
            # Asking once per (participant, venue) also removed the single
            # largest block of wasted requests in the run: the second
            # credential re-walked the same per-currency transfer history the
            # first had already fetched - 33 requests and 11.8s on one live
            # account, to produce nothing.
            account_wide = getattr(venue, "CASH_FLOWS_ARE_ACCOUNT_WIDE", False)
            sync_flows = (not account_wide
                          or flow_collector.get(exchange_id) is credential)

            work.append({
                "participant_id": participant_id,
                "credential": credential,
                "venue": venue,
                "label": label,
                "sync_flows": sync_flows,
            })

    return work, skipped


def _flow_collectors(credentials: list[dict]) -> dict:
    """
    For one participant, which credential collects each account-wide venue's
    transfers. Keyed by exchange id; the value is the credential row itself.

    Choosing has to be deliberate, because for some venues only ONE of a
    participant's credentials can read the history at all. Coinbase's lives
    behind the v2 transactions endpoint, reached with the ordinary Advanced
    Trade key; an INTX perp key sees no v2 accounts and returns an empty
    result that is indistinguishable from "never deposited". Handing the job
    to that credential stops cash flow collection with no error, no failed
    step, and a green run - and every participant's return silently stops
    being adjusted for funding.

    So the adapter names the account_type that can do it
    (CASH_FLOWS_ACCOUNT_TYPE) and that credential is preferred. If the
    participant hasn't registered one, the first credential for the venue is
    used anyway: a venue that might answer beats one guaranteed not to be
    asked.
    """
    collectors: dict = {}

    for credential in credentials:
        exchange_id = credential.get("exchange") or "coinbase"
        if not credential.get("api_key"):
            continue

        try:
            venue = venues.get(exchange_id)
        except venues.UnknownVenue:
            continue

        if not getattr(venue, "CASH_FLOWS_ARE_ACCOUNT_WIDE", False):
            continue

        preferred = getattr(venue, "CASH_FLOWS_ACCOUNT_TYPE", None)
        chosen = collectors.get(exchange_id)

        if chosen is None:
            collectors[exchange_id] = credential
        elif (preferred is not None
              and credential.get("account_type") == preferred
              and chosen.get("account_type") != preferred):
            collectors[exchange_id] = credential

    return collectors


def sync_one_credential(item: dict) -> dict:
    """
    Sync one credential, and never raise.

    Every step is independently guarded and the outcome comes back as
    counters, so this can be handed to a thread pool: a worker that raised
    would take down the pool and cost everyone else their run, which is the
    opposite of what the per-credential isolation is for.

    Returned keys mirror the run summary - orders, snapshots, flows,
    task_failures - plus `failed`, which is 1 when this credential produced
    no balance snapshot. That is the line because the snapshot is the only
    datum that cannot be recovered later: orders and cash flows resume from
    their stored timestamps on the next run, but the equity curve point for
    12:15 is gone for good.
    """
    result = {"orders": 0, "snapshots": 0, "flows": 0,
              "task_failures": 0, "failed": 0}

    participant_id = item["participant_id"]
    label = item["label"]

    if item.get("unknown_venue"):
        result["task_failures"] = 1
        result["failed"] = 1
        return result

    credential, venue = item["credential"], item["venue"]

    try:
        exchange = venue.build_from_credential(credential)
    except Exception as e:
        # Can't build the client - nothing else is possible for this row
        result["task_failures"] += 1
        result["failed"] = 1
        log_fetch_error(participant_id, e, label)
        return result

    # Orders and balances are independent. A malformed order or a constraint
    # violation shouldn't also cost this participant their equity curve point
    # - that datum can't be backfilled later.
    try:
        written = sync_orders(participant_id, credential, exchange, venue)
        result["orders"] = written
        logger.info("%s (%s): %d orders", participant_id, label, written)
    except Exception as e:
        result["task_failures"] += 1
        log_fetch_error(participant_id, e, label)

    try:
        sync_balance_snapshot(participant_id, credential, exchange, venue)
        result["snapshots"] = 1
        logger.info("%s (%s): snapshot recorded", participant_id, label)
    except Exception as e:
        # The unrecoverable one - see the docstring.
        result["task_failures"] += 1
        result["failed"] = 1
        log_fetch_error(participant_id, e, label)

    if item.get("sync_flows", True):
        try:
            moved = sync_cash_flows(participant_id, credential, exchange, venue)
            result["flows"] = moved
            if moved:
                logger.info("%s (%s): %d cash flow(s)",
                            participant_id, label, moved)
        except Exception as e:
            result["task_failures"] += 1
            log_fetch_error(participant_id, e, label)
    else:
        logger.debug("%s (%s): cash flows already collected for this venue",
                     participant_id, label)

    return result


# How many credentials to sync at once. Almost all of a run is spent waiting
# on exchange HTTP, so threads are the right tool here even under the GIL.
#
# Measured against live accounts: 5 credentials took 34.0s serially and 19.0s
# at 8 workers. The gain is capped there only because one participant's
# transfer history dominates and there is nothing left to overlap it with -
# the more participants there are, the better this scales, which is exactly
# the direction the competition grows.
#
# Each credential builds its OWN ccxt instance, so ccxt's rate limiter (which
# is per-instance and not thread-safe) is never shared between threads. The
# only shared state is the market and ticker cache in venue_common, which
# takes a lock.
#
# Set SYNC_WORKERS=1 for a strictly serial run.
_DEFAULT_WORKERS = 8


def _worker_count(work_items: int) -> int:
    """
    How many threads to use, from SYNC_WORKERS, clamped to something sane.

    Never more threads than there is work, and never fewer than one. A bad
    value falls back to the default rather than taking down the run - the
    sync working matters more than the concurrency being exactly as asked.
    """
    raw = os.getenv("SYNC_WORKERS")
    workers = _DEFAULT_WORKERS

    if raw:
        try:
            workers = int(raw)
        except ValueError:
            logger.warning("SYNC_WORKERS=%r is not an integer - using %d",
                           raw, _DEFAULT_WORKERS)
        else:
            if workers < 1:
                logger.warning("SYNC_WORKERS=%d is below 1 - using 1", workers)
                workers = 1

    return max(1, min(workers, work_items))


def sync_all_to_supabase() -> dict:
    """
    For every participant, sync every venue they've registered.

    Each credential is handled independently: a participant's expired perp key
    shouldn't cost them their spot sync, and no single participant's failure
    should cost everyone else their run. That independence is what lets the
    credentials run concurrently - see sync_one_credential().

    Returns a summary the caller uses to set an exit code. The run completing
    is not the same as the run working, and on a scheduled host the exit code
    is the only signal anything watches.

    A credential counts as FAILED when it produced no balance snapshot. That
    is the line because the snapshot is the only datum that can't be recovered
    later: orders and cash flows resume from their stored timestamps on the
    next run, but the equity curve point for 12:15 is gone for good. A
    credential whose orders failed while its snapshot succeeded has lost
    nothing permanent, so it doesn't count against the threshold - the error
    is still logged and still in fetch_errors either way.
    """
    participants = get_active_participants()
    credentials_by_participant = get_all_api_keys()

    work, skipped = _plan_credential_work(participants, credentials_by_participant)

    totals = {"orders": 0, "snapshots": 0, "flows": 0,
              "task_failures": 0, "failed": 0}

    if work:
        workers = _worker_count(len(work))
        logger.info("Syncing %d credential(s) with %d worker(s)",
                    len(work), workers)

        if workers == 1:
            results = [sync_one_credential(item) for item in work]
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="sync") as pool:
                results = list(pool.map(sync_one_credential, work))

        for result in results:
            for key in totals:
                totals[key] += result[key]

    attempted = len(work)

    logger.info("Done: %d orders upserted, %d snapshots recorded, %d cash "
                "flow(s). %d of %d credential(s) failed, %d step(s) failed in "
                "total, %d row(s) skipped for having no key.",
                totals["orders"], totals["snapshots"], totals["flows"],
                totals["failed"], attempted, totals["task_failures"], skipped)

    return {
        "attempted": attempted,
        "failed": totals["failed"],
        "task_failures": totals["task_failures"],
        "skipped": skipped,
        "orders": totals["orders"],
        "snapshots": totals["snapshots"],
        "flows": totals["flows"],
    }



if __name__ == "__main__":
    # Configure logging HERE, not at import: a library that configures the
    # root logger on import hijacks the settings of anything that imports it.
    # The entrypoint owns this decision.
    #
    # Level defaults to INFO so the operational lines are visible; set
    # LOG_LEVEL=DEBUG to include the per-symbol pricing detail from coinbase.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # httpx logs every Supabase call at INFO - two lines per participant per
    # venue, which buries the sync's own output. Warnings still come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    summary = sync_all_to_supabase()

    # Without a non-zero exit the process always reports success, so a sync
    # that has silently stopped working looks identical to a healthy one and
    # nothing the host offers (alerts, retries, run history) can tell them
    # apart. But failing on ANY error is just as useless in the other
    # direction - see exit_code_for() for why.
    threshold = _failure_threshold()
    code = exit_code_for(summary["attempted"], summary["failed"], threshold)

    if code:
        if summary["attempted"] == 0:
            logger.error("Exiting non-zero: no credentials to sync at all - "
                         "check that participants and participant_api_keys "
                         "are populated and is_active is set")
        else:
            logger.error("Exiting non-zero: %d of %d credential(s) produced no "
                         "snapshot, at or above the %.0f%% threshold",
                         summary["failed"], summary["attempted"], threshold * 100)
    elif summary["failed"]:
        logger.warning("%d of %d credential(s) failed, below the %.0f%% "
                       "threshold - run still reported as successful. See "
                       "fetch_errors for detail.",
                       summary["failed"], summary["attempted"], threshold * 100)

    sys.exit(code)
