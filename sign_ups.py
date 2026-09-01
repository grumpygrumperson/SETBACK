import csv
import logging
import os
import sys
from datetime import datetime, timezone

from supabase import create_client
from dotenv import load_dotenv

import signup_crypto
import venues
from venue_common import get_fernet

# Reads participant signups from CSV, verifies their exchange credentials,
# and stores them encrypted across `participants` and `participant_api_keys`.

load_dotenv()

logger = logging.getLogger(__name__)

# Named here rather than left to fail inside the library. Getting the
# environment wrong is the most common way this script goes wrong, and
# create_client's own error doesn't say which variable was missing.
_missing = [n for n in ("SUPABASE_URL", "SUPABASE_KEY") if not os.getenv(n)]
if _missing:
    raise RuntimeError(
        "Missing required environment variable(s): " + ", ".join(_missing) +
        ". Set them in your .env. FERNET_KEY is also required, and must be "
        "the same key the sync uses, or nothing registered here will decrypt."
    )

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

SIGNUPS_CSV = "participant_signups.csv"

def _clean(value) -> str:
    """
    Read one CSV cell, treating blanks as absent.

    This used to be load-bearing against pandas: pandas turns an empty CSV
    field into float('nan'), and nan is TRUTHY - so the obvious
    `row.get(col) or None` keeps it, and str(nan) then gets stored as the
    literal string "nan". That is exactly what happened to api_passphrase:
    every Coinbase row here held an encrypted "nan".

    It did no harm only because ccxt.coinbase ignores `password`. On
    coinbaseexchange or coinbaseinternational, which require it, the venue
    would have been handed "nan" as the passphrase and failed to authenticate
    with nothing to say why.

    The reader is now csv.DictReader, which yields plain strings and cannot
    produce a nan at all - but this stays, because it also strips whitespace
    and still catches a "nan" written into a CSV by whatever exported it.
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

    # What the venue said this key can do, recorded at the moment it was
    # proven read-only. Stored so the answer can be re-checked on a schedule
    # instead of re-derived on every sync - permissions CAN widen after
    # signup, and 200 credentials re-checked daily is 200 requests rather than
    # 4,800. Venues that expose nothing comparable (Lighter, where the token
    # format is the guarantee) simply store null.
    permissions = details.get("permissions") or None

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
            "permissions": permissions,
            # A real timestamp, not the string "now()" - PostgREST sends JSON
            # and Postgres will not evaluate a function name arriving as text.
            "permissions_checked_at": (
                datetime.now(timezone.utc).isoformat() if permissions else None
            ),
            "is_active": True,
        },
        on_conflict="participant_id,exchange,account_type",
    ).execute()


def import_signups(path: str = SIGNUPS_CSV) -> None:
    """
    Import every row of the signup CSV. One bad row is reported and skipped
    rather than aborting the whole import.

    Read with the standard library rather than pandas. pandas was the only
    thing pulling ~60MB of NumPy into a deploy that never runs this file -
    the Railway service starts post_to_supabase.py - and its nan-for-blank
    behaviour is what put an encrypted literal "nan" into every Coinbase
    passphrase. DictReader yields plain strings, so a blank cell is just "".

    utf-8-sig because a CSV exported from Excel starts with a byte-order
    mark, which would otherwise become part of the first column's NAME and
    make row["username"] a KeyError on every row.
    """
    with open(path, newline="", encoding="utf-8-sig") as handle:
        signups = list(csv.DictReader(handle))

    imported = 0

    for row in signups:
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


# ---------------------------------------------------------------------------
# The signup form's inbox
#
# The CSV path above requires an operator to collect plaintext credentials by
# hand and keep them in a file on a laptop. This is the replacement: the
# browser encrypts to SIGNUP_PUBLIC_KEY, the database stores only ciphertext,
# and this drains it.
# ---------------------------------------------------------------------------

# How many times a submission is retried before it is given up on.
#
# Most verification failures are PERMANENT - a key that can trade will still
# be able to trade in an hour - so this exists for the transient ones: a
# venue timing out, a rate limit, a Supabase blip. Three is enough for that
# and small enough that a credential nobody can use is not retained for long,
# which matters because the row is holding a live secret until it resolves.
MAX_IMPORT_ATTEMPTS = 3

# One request's worth of submissions. The table is drained to empty by
# repeating, so this only bounds how much is held in memory at once.
_PENDING_PAGE = 200


def fetch_pending_signups(limit: int = _PENDING_PAGE) -> list[dict]:
    """
    Unresolved submissions, OLDEST FIRST.

    The order is load-bearing. register_credential upserts on
    (participant_id, exchange, account_type), so when someone submits twice -
    which is exactly what a participant does after being told their first key
    was rejected - the LAST row processed wins. Oldest first makes that the
    newest submission. Newest first would silently keep the broken key.
    """
    return (supabase.table("pending_signups")
            .select("id,display_name,email,exchange,account_type,"
                    "ciphertext,attempts")
            .eq("status", "pending")
            .order("id")
            .limit(limit)
            .execute().data) or []


def _resolve_signup(row_id: int, status: str, error: str = None) -> None:
    """
    Close out a submission and DESTROY its payload.

    Clearing ciphertext is the point of this function, not a detail of it. An
    imported credential now lives Fernet-encrypted in participant_api_keys; a
    rejected one is a credential nobody will ever use. Either way what is
    left in pending_signups is a secret with no remaining purpose, and the
    database has a check constraint that refuses to let a resolved row keep
    one - so a bug here fails loudly rather than quietly hoarding keys.
    """
    supabase.table("pending_signups").update({
        "status": status,
        "ciphertext": None,
        "last_error": error,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row_id).execute()


def _defer_signup(row_id: int, attempts: int, error: str) -> None:
    """Leave a submission pending for another run, recording why."""
    supabase.table("pending_signups").update({
        "attempts": attempts,
        "last_error": error,
    }).eq("id", row_id).execute()


def import_pending_signups(limit: int = _PENDING_PAGE) -> dict:
    """
    Decrypt, verify and register every pending submission.

    One row failing must not stop the others: a submission encrypted to a
    retired public key, or carrying a key that turns out to be trade-capable,
    is that participant's problem and nobody else's.

    Returns counts for the caller to report.
    """
    pending = fetch_pending_signups(limit)
    if not pending:
        logger.info("No pending signups")
        return {"pending": 0, "imported": 0, "rejected": 0, "deferred": 0}

    counts = {"pending": len(pending), "imported": 0,
              "rejected": 0, "deferred": 0}

    for row in pending:
        label = f"#{row['id']} {row.get('display_name') or '?'}"

        # A submission that will not decrypt is never going to decrypt.
        # Retrying it would only keep an unreadable payload in the table for
        # longer, so this rejects on the first attempt rather than counting.
        try:
            payload = signup_crypto.decrypt(row["ciphertext"] or "")
        except (ValueError, RuntimeError) as e:
            logger.error("%s: %s", label, e)
            _resolve_signup(row["id"], "rejected", f"decryption failed: {e}")
            counts["rejected"] += 1
            continue

        # The envelope is authenticated; the plaintext columns are not. So
        # the venue comes from inside the ciphertext, and a tampered
        # `exchange` column cannot route a Coinbase key to another adapter.
        # Identity is taken from the columns, because it was never secret and
        # is not in the envelope.
        credential = {
            "username": row["display_name"],
            "email": row["email"],
            "api_key": payload.get("api_key"),
            "api_secret": payload.get("api_secret"),
            "api_passphrase": payload.get("api_passphrase"),
            "exchange": payload.get("exchange"),
            "account_type": payload.get("account_type"),
        }

        try:
            # Enforces read-only. A trade- or transfer-capable Coinbase key
            # and a Lighter wallet private key are both refused here, by the
            # same code path the CSV import uses.
            details = verify_and_describe(credential)
            participant_id = register_participant(credential)
            register_credential(participant_id, credential, details)

        except Exception as e:
            attempts = (row.get("attempts") or 0) + 1
            reason = f"{type(e).__name__}: {e}"

            if attempts >= MAX_IMPORT_ATTEMPTS:
                logger.error("%s: giving up after %d attempt(s) - %s",
                             label, attempts, reason)
                _resolve_signup(row["id"], "rejected", reason)
                counts["rejected"] += 1
            else:
                logger.warning("%s: attempt %d/%d failed - %s",
                               label, attempts, MAX_IMPORT_ATTEMPTS, reason)
                _defer_signup(row["id"], attempts, reason)
                counts["deferred"] += 1
            continue

        _resolve_signup(row["id"], "imported")
        counts["imported"] += 1
        logger.info("%s: registered (%s/%s)", label,
                    credential["exchange"], details["account_type"])

    logger.info("Pending signups: %d imported, %d rejected, %d deferred",
                counts["imported"], counts["rejected"], counts["deferred"])
    return counts


if __name__ == "__main__":
    # Warnings here are the point of running this interactively - an expiring
    # Lighter token or a fee-paying Premium account both surface as WARNING.
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)-7s %(name)s | %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # The form is the way in now; the CSV is the legacy path, kept because it
    # is how the existing participants were registered and how a signup can
    # be repaired by hand.
    if "--csv" in sys.argv:
        import_signups()
    else:
        import_pending_signups()
