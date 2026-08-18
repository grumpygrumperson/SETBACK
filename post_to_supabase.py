import os
from supabase import create_client
from dotenv import load_dotenv
from coinbase import build_exchange, closed_orders

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))


def get_active_participants() -> list[dict]:
    """
    Fetches all active participants from the Supabase database.
    """
    return supabase.table("participants_credentials").select("*").execute().data


def get_last_synced_timestamp(participant_id) -> int:
    """
    Return the millisecond timestamp to resume this participant's sync from,
    i.e. one past their most recently stored order. Returns None if they have
    no stored orders yet, which lets closed_orders() fall back to its own
    start-of-competition default.
    """
    response = (
        supabase.table("trade_metrics")
        .select("timestamp")
        .eq("participant_id", participant_id)
        .order("timestamp", desc=True)
        .limit(1)
        .execute()
    )

    rows = response.data or []
    if rows and rows[0].get("timestamp") is not None:
        # +1ms so the boundary order isn't refetched on every run
        return int(rows[0]["timestamp"]) + 1
    return None


def log_fetch_error(participant_id, error: Exception) -> None:
    """
    Record a per-participant failure so it's visible after an unattended run -
    a participant whose keys expire mid-competition would otherwise just stop
    appearing with nothing but stdout to say why.
    """
    print(f"Failed to sync user {participant_id}. Error: {error}")
    try:
        supabase.table("fetch_errors").insert({
            "participant_id": participant_id,
            "error_message": str(error)[:500],
        }).execute()
    except Exception as e:
        print(f"Could not record fetch error for {participant_id}: {e}")


def sync_all_trades_to_supabase():
    """
    Fetch each participant's closed orders since their last synced order and
    upsert them into trade_metrics, tagged with their Supabase UUID.

    Each participant is uploaded independently: one participant's bad data or
    expired keys shouldn't cost everyone else their run.
    """
    participants = get_active_participants()
    total_rows = 0

    for participant in participants:
        participant_id = participant.get('id')
        api_key = participant.get('api_key')
        api_secret = participant.get('api_secret')

        if not api_key or not api_secret:
            continue

        try:
            exchange = build_exchange(
                api_key,
                api_secret,
                participant.get('exchange') or 'coinbase',
            )

            since = get_last_synced_timestamp(participant_id)
            user_orders = closed_orders(exchange, since=since)

            if not user_orders:
                print(f"No new orders for user {participant_id}")
                continue

            for order in user_orders:
                order['participant_id'] = participant_id

            response = supabase.table("trade_metrics").upsert(
                user_orders,
                on_conflict="participant_id,order_id",
                ignore_duplicates=False,
            ).execute()

            written = len(response.data or [])
            total_rows += written
            print(f"Synced {written} orders for user {participant_id}")

        except Exception as e:
            log_fetch_error(participant_id, e)

    print(f"SUCCESS: Supabase inserted/updated {total_rows} rows in total.")


if __name__ == "__main__":
    sync_all_trades_to_supabase()