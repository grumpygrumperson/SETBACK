import os
import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

from coinbase import build_exchange, find_perp_portfolio_uuid, _get_fernet

# Reads participant signups from CSV, verifies their exchange credentials,
# and stores them encrypted across `participants` and `participant_api_keys`.

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

SIGNUPS_CSV = "participant_signups.csv"


def encrypt_value(value) -> str:
    """
    Encrypt a value for storage.

    Uses the shared MultiFernet from coinbase rather than building a bare
    Fernet here: MultiFernet always encrypts with the CURRENT key, so a
    signup during a rotation writes new-key ciphertext instead of quietly
    adding rows under the key being retired.
    """
    return _get_fernet().encrypt(str(value).encode()).decode()


def register_participant(row) -> str:
    """
    Insert (or refresh) the participant's identity row and return their UUID.

    Keyed on email so re-running the import doesn't create duplicates.
    """
    response = supabase.table("participants").upsert(
        {
            "display_name": row["username"],
            "email": row["email"],
        },
        on_conflict="email",
    ).execute()

    return response.data[0]["id"]


def verify_and_describe(row) -> dict:
    """
    Check the credentials actually work before storing them, and resolve the
    perp portfolio UUID while we have a live connection.

    Doing this at signup means a participant with bad keys finds out while
    they can still fix it, rather than silently missing from the leaderboard.
    Raises if the credentials don't authenticate.
    """
    account_type = (row.get("account_type") or "spot").strip().lower()
    passphrase = row.get("api_passphrase") or None

    # Plaintext at this point - they're encrypted further down, after they've
    # been proven to work.
    exchange = build_exchange(
        row["api_key"],
        row["api_secret"],
        row.get("exchange") or "coinbase",
        encrypted=False,
        passphrase=passphrase,
    )

    portfolio_uuid = None
    if account_type == "perp":
        # Costs one API call, run once here rather than on every sync.
        portfolio_uuid = find_perp_portfolio_uuid(exchange)
        if not portfolio_uuid:
            raise ValueError(
                "No perp portfolio visible to these credentials - the key may "
                "be scoped to the spot portfolio instead"
            )
    else:
        # Cheap auth check. Kept venue-agnostic - no exchange-specific params -
        # since participants may register on exchanges other than Coinbase.
        exchange.fetch_balance()

    return {"account_type": account_type, "portfolio_uuid": portfolio_uuid,
            "passphrase": passphrase}


def register_credential(participant_id, row, details: dict) -> None:
    """
    Store one venue's credentials, encrypted, against the participant.

    Keyed on (participant_id, exchange, account_type) so re-running the import
    updates a participant's keys rather than duplicating them.
    """
    supabase.table("participant_api_keys").upsert(
        {
            "participant_id": participant_id,
            "exchange": row.get("exchange") or "coinbase",
            "account_type": details["account_type"],
            "api_key": encrypt_value(row["api_key"]),
            "api_secret": encrypt_value(row["api_secret"]),
            "api_passphrase": encrypt_value(details["passphrase"]) if details["passphrase"] else None,
            "portfolio_uuid": details["portfolio_uuid"],
            "is_active": True,
        },
        on_conflict="participant_id,exchange,account_type",
    ).execute()


def import_signups(path: str = SIGNUPS_CSV) -> None:
    """
    Import every row of the signup CSV. One bad row is reported and skipped
    rather than aborting the whole import.
    """
    signups = pd.read_csv(path)
    imported = 0

    for _, row in signups.iterrows():
        try:
            details = verify_and_describe(row)
            participant_id = register_participant(row)
            register_credential(participant_id, row, details)

            imported += 1
            print(f"Registered {row['username']} ({details['account_type']})")

        except Exception as e:
            print(f"Skipped {row.get('username')}: {e}")

    print(f"Imported {imported} of {len(signups)} signups.")


if __name__ == "__main__":
    import_signups()
