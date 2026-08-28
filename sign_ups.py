import logging
import os

import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

import venues
from venue_common import get_fernet

# Reads participant signups from CSV, verifies their exchange credentials,
# and stores them encrypted across `participants` and `participant_api_keys`.

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

SIGNUPS_CSV = "participant_signups.csv"

def _clean(value) -> str:
    """
    Read one CSV cell, treating blanks as absent.

    pandas turns an empty CSV field into float('nan'), and nan is TRUTHY - so
    the obvious `row.get(col) or None` keeps it, and str(nan) then gets stored
    as the literal string "nan". That is exactly what happened to
    api_passphrase: every Coinbase row here held an encrypted "nan".

    It did no harm only because ccxt.coinbase ignores `password`. On
    coinbaseexchange or coinbaseinternational, which require it, the venue
    would have been handed "nan" as the passphrase and failed to authenticate
    with nothing to say why.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ('nan', 'none', 'null'):
        return None
    return text


def encrypt_value(value) -> str:
    """
    Encrypt a value for storage.

    Uses the shared MultiFernet rather than building a bare Fernet here:
    MultiFernet always encrypts with the CURRENT key, so a signup during a
    rotation writes new-key ciphertext instead of quietly adding rows under
    the key being retired.
    """
    return get_fernet().encrypt(str(value).encode()).decode()


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
    Check a credential actually works before storing it, and resolve the
    venue's account handle while we have a live connection.

    Pure dispatch: every adapter implements verify_credential(), so the
    venue-specific checks live with the venue rather than as branches here.
    Coinbase resolves an INTX portfolio UUID; Lighter refuses a 'spot'
    registration and reads its account index out of the token.

    Doing this at signup means a participant with bad credentials finds out
    while they can still fix it, rather than silently missing from the
    leaderboard.
    """
    venue = venues.get(_clean(row.get("exchange")) or "coinbase")

    return venue.verify_credential({
        "api_key": _clean(row.get("api_key")),
        "api_secret": _clean(row.get("api_secret")),
        "api_passphrase": _clean(row.get("api_passphrase")),
        "account_type": _clean(row.get("account_type")),
        "exchange": _clean(row.get("exchange")) or "coinbase",
    })


def register_credential(participant_id, row, details: dict) -> None:
    """
    Store one venue's credentials, encrypted, against the participant.

    Keyed on (participant_id, exchange, account_type) so re-running the import
    updates a participant's keys rather than duplicating them.
    """
    api_secret = _clean(row.get("api_secret"))

    supabase.table("participant_api_keys").upsert(
        {
            "participant_id": participant_id,
            "exchange": _clean(row.get("exchange")) or "coinbase",
            "account_type": details["account_type"],
            "api_key": encrypt_value(_clean(row["api_key"])),
            # Null on single-credential venues. Lighter authenticates with one
            # value, held in api_key; storing a placeholder here would be a
            # value the sync could later hand to an exchange.
            "api_secret": encrypt_value(api_secret) if api_secret else None,
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
            venue = _clean(row.get("exchange")) or "coinbase"
            print(f"Registered {row['username']} "
                  f"({venue}/{details['account_type']})")

        except Exception as e:
            print(f"Skipped {row.get('username')}: {e}")

    print(f"Imported {imported} of {len(signups)} signups.")


if __name__ == "__main__":
    # Warnings here are the point of running this interactively - an expiring
    # Lighter token or a fee-paying Premium account both surface as WARNING.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)-7s %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    import_signups()
