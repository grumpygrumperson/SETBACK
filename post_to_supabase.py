import logging
import os
import sys
from supabase import create_client
from dotenv import load_dotenv
from coinbase import (build_exchange, closed_orders, get_account_totals_usdc,
                      get_cash_flows)

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
    Fetch a participant's active credential rows - one per venue, so a
    participant trading both spot and perps comes back as two rows, each
    carrying its own passphrase and portfolio UUID.
    """
    response = (
        supabase.table("participant_api_keys")
        .select("*")
        .eq("participant_id", participant_id)
        .eq("is_active", True)
        .execute()
    )
    return response.data or []


def get_last_synced_timestamp(participant_id, account_type: str) -> int:
    """
    Return the millisecond timestamp to resume this participant's sync from,
    i.e. one past their most recently stored order for this venue.

    `account_type` is not optional: without it a participant's perp sync would
    resume from their last spot trade and silently skip everything between.

    Returns None when there's nothing stored yet, which lets closed_orders()
    fall back to its own start-of-competition default.
    """
    response = (
        supabase.table("trade_metrics")
        .select("timestamp")
        .eq("participant_id", participant_id)
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


def log_fetch_error(participant_id, error: Exception, account_type: str = None) -> None:
    """
    Record a per-credential failure so it's visible after an unattended run -
    a participant whose keys expire mid-competition would otherwise just stop
    appearing with nothing but stdout to say why.
    """
    label = f"{participant_id} ({account_type})" if account_type else str(participant_id)
    logger.error("Failed to sync %s: %s", label, error)
    try:
        supabase.table("fetch_errors").insert({
            "participant_id": participant_id,
            "error_message": f"[{account_type or 'unknown'}] {error}"[:500],
        }).execute()
    except Exception as e:
        # Never let error logging take down the run for everyone else
        logger.error("Could not record fetch error for %s: %s", label, e)


def sync_orders(participant_id, credential: dict, exchange) -> int:
    """
    Upsert one credential's closed orders into trade_metrics. Returns the
    number of rows Supabase accepted.
    """
    account_type = credential.get("account_type")
    portfolio_uuid = credential.get("portfolio_uuid")

    since = get_last_synced_timestamp(participant_id, account_type)
    orders = closed_orders(exchange, since=since, portfolio_uuid=portfolio_uuid)

    if not orders:
        return 0

    for order in orders:
        order["participant_id"] = participant_id
        # The CREDENTIAL is authoritative, not the order's product_type.
        # Orders are fetched with a portfolio-scoped key, so they can only
        # come from that venue - whereas Coinbase labels INTX perpetuals
        # 'FUTURE' with no contract_expiry_type, which reads as a dated
        # future. Storing them under a different account_type than the one
        # get_last_synced_timestamp() queries makes the resume point invisible
        # and re-fetches the whole history every run.
        order["account_type"] = account_type

    response = supabase.table("trade_metrics").upsert(
        orders,
        on_conflict="participant_id,account_type,order_id",
        ignore_duplicates=False,
    ).execute()

    # response.data is None on some error paths - don't let the count raise
    # and mask the real failure.
    return len(response.data or [])


def sync_balance_snapshot(participant_id, credential: dict, exchange) -> None:
    """
    Record one point on this credential's equity curve.

    Both the spot and perp valuations return a 'total_usdc' headline figure,
    so they store identically; whatever else each returns is kept in `detail`
    rather than discarded, since it can't be backfilled later.
    """
    account_type = credential.get("account_type")
    portfolio_uuid = credential.get("portfolio_uuid")

    snapshot = get_account_totals_usdc(
        exchange,
        account_type=account_type,
        portfolio_uuid=portfolio_uuid,
    )

    detail = {k: v for k, v in snapshot.items()
              if k not in ("timestamp", "datetime", "account_type", "total_usdc")}

    # upsert, not insert: the table is unique on
    # (participant_id, account_type, timestamp), so a retry landing on the
    # same millisecond would otherwise raise instead of being a no-op.
    supabase.table("balance_snapshots").upsert({
        "participant_id": participant_id,
        "account_type": account_type,
        "timestamp": snapshot.get("timestamp"),
        "total_usdc": snapshot.get("total_usdc"),
        "detail": detail,
    }, on_conflict="participant_id,account_type,timestamp").execute()


def sync_cash_flows(participant_id, credential: dict, exchange) -> int:
    """
    Record external deposits and withdrawals for one credential.

    Returns are meaningless without these: money arriving in an account looks
    identical to money earned in it. Stored raw and unjudged - deciding which
    transfers are internal happens in the metrics layer, where a participant's
    venues can be compared against each other.
    """
    account_type = credential.get("account_type")

    since = get_last_flow_timestamp(participant_id, account_type)
    flows = get_cash_flows(exchange, since=since)

    if not flows:
        return 0

    for flow in flows:
        flow["participant_id"] = participant_id
        flow["account_type"] = account_type

    response = supabase.table("cash_flows").upsert(
        flows,
        on_conflict="participant_id,account_type,transfer_id",
        ignore_duplicates=False,
    ).execute()

    return len(response.data or [])


def get_last_flow_timestamp(participant_id, account_type: str) -> int:
    """
    Resume point for this credential's transfer history, mirroring
    get_last_synced_timestamp for orders.
    """
    response = (
        supabase.table("cash_flows")
        .select("timestamp")
        .eq("participant_id", participant_id)
        .eq("account_type", account_type)
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )

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


def sync_all_to_supabase() -> dict:
    """
    For every participant, sync every venue they've registered.

    Each credential is handled independently: a participant's expired perp key
    shouldn't cost them their spot sync, and no single participant's failure
    should cost everyone else their run.

    Returns a summary the caller uses to set an exit code. The run completing
    is not the same as the run working, and on a scheduled host the exit code
    is the only signal anything watches.

    A credential counts as FAILED when it produced no balance snapshot.
    That's the line because the snapshot is the only datum that can't be
    recovered later: orders and cash flows resume from their stored
    timestamps on the next run, but the equity curve point for 12:15 is gone
    for good. A credential whose orders failed while its snapshot succeeded
    has lost nothing permanent, so it doesn't count against the threshold -
    the error is still logged and still in fetch_errors either way.
    """
    participants = get_active_participants()
    total_orders = 0
    total_snapshots = 0
    total_flows = 0
    task_failures = 0        # every failed step, for reporting
    attempted = 0            # credentials we had keys for
    failed = 0               # credentials that produced no snapshot
    skipped = 0              # rows with no key stored at all

    for participant in participants:
        participant_id = participant.get("id")

        for credential in get_api_keys(participant_id):
            account_type = credential.get("account_type")

            if not credential.get("api_key") or not credential.get("api_secret"):
                # A registration problem rather than a sync failure, but not
                # silent either - an active row with no key never syncs.
                skipped += 1
                logger.warning("%s (%s): active credential row has no key stored",
                               participant_id, account_type)
                continue

            attempted += 1

            try:
                exchange = build_exchange(
                    credential["api_key"],
                    credential["api_secret"],
                    credential.get("exchange") or "coinbase",
                    passphrase=credential.get("api_passphrase"),
                    portfolio_uuid=credential.get("portfolio_uuid"),
                )
            except Exception as e:
                # Can't build the client - nothing else is possible for this row
                task_failures += 1
                failed += 1
                log_fetch_error(participant_id, e, account_type)
                continue

            # Orders and balances are independent. A malformed order or a
            # constraint violation shouldn't also cost this participant their
            # equity curve point - that datum can't be backfilled later.
            try:
                written = sync_orders(participant_id, credential, exchange)
                total_orders += written
                logger.info("%s (%s): %d orders", participant_id, account_type, written)
            except Exception as e:
                task_failures += 1
                log_fetch_error(participant_id, e, account_type)

            try:
                sync_balance_snapshot(participant_id, credential, exchange)
                total_snapshots += 1
                logger.info("%s (%s): snapshot recorded", participant_id, account_type)
            except Exception as e:
                # The unrecoverable one - see the docstring.
                task_failures += 1
                failed += 1
                log_fetch_error(participant_id, e, account_type)

            try:
                moved = sync_cash_flows(participant_id, credential, exchange)
                total_flows += moved
                if moved:
                    logger.info("%s (%s): %d cash flow(s)",
                                participant_id, account_type, moved)
            except Exception as e:
                task_failures += 1
                log_fetch_error(participant_id, e, account_type)

    logger.info("Done: %d orders upserted, %d snapshots recorded, %d cash "
                "flow(s). %d of %d credential(s) failed, %d step(s) failed in "
                "total, %d row(s) skipped for having no key.",
                total_orders, total_snapshots, total_flows,
                failed, attempted, task_failures, skipped)

    return {
        "attempted": attempted,
        "failed": failed,
        "task_failures": task_failures,
        "skipped": skipped,
        "orders": total_orders,
        "snapshots": total_snapshots,
        "flows": total_flows,
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
