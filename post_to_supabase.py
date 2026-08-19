import os
from supabase import create_client
from dotenv import load_dotenv
from coinbase import build_exchange, closed_orders, get_account_totals_usdc

load_dotenv()

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
    print(f"Failed to sync {label}. Error: {error}")
    try:
        supabase.table("fetch_errors").insert({
            "participant_id": participant_id,
            "error_message": f"[{account_type or 'unknown'}] {error}"[:500],
        }).execute()
    except Exception as e:
        # Never let error logging take down the run for everyone else
        print(f"Could not record fetch error for {label}: {e}")


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
        # closed_orders reads account_type off each order's product_type, but
        # falls back to None if Coinbase didn't report one. A null here would
        # defeat the (participant_id, account_type, order_id) constraint -
        # Postgres treats nulls as distinct - and re-insert duplicates every
        # run, so fall back to the venue the credential belongs to.
        if not order.get("account_type"):
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

    supabase.table("balance_snapshots").insert({
        "participant_id": participant_id,
        "account_type": account_type,
        "timestamp": snapshot.get("timestamp"),
        "total_usdc": snapshot.get("total_usdc"),
        "detail": detail,
    }).execute()


def sync_all_to_supabase():
    """
    For every participant, sync every venue they've registered.

    Each credential is handled independently: a participant's expired perp key
    shouldn't cost them their spot sync, and no single participant's failure
    should cost everyone else their run.
    """
    participants = get_active_participants()
    total_orders = 0
    total_snapshots = 0
    failures = 0

    for participant in participants:
        participant_id = participant.get("id")

        for credential in get_api_keys(participant_id):
            account_type = credential.get("account_type")

            if not credential.get("api_key") or not credential.get("api_secret"):
                continue

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
                failures += 1
                log_fetch_error(participant_id, e, account_type)
                continue

            # Orders and balances are independent. A malformed order or a
            # constraint violation shouldn't also cost this participant their
            # equity curve point - that datum can't be backfilled later.
            try:
                written = sync_orders(participant_id, credential, exchange)
                total_orders += written
                print(f"{participant_id} ({account_type}): {written} orders")
            except Exception as e:
                failures += 1
                log_fetch_error(participant_id, e, account_type)

            try:
                sync_balance_snapshot(participant_id, credential, exchange)
                total_snapshots += 1
                print(f"{participant_id} ({account_type}): snapshot recorded")
            except Exception as e:
                failures += 1
                log_fetch_error(participant_id, e, account_type)

    print(f"Done: {total_orders} orders upserted, {total_snapshots} snapshots "
          f"recorded, {failures} credential(s) failed.")


if __name__ == "__main__":
    sync_all_to_supabase()
