import logging
import os

import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

import coinbase
import lighter
from coinbase import _get_fernet

# Reads participant signups from CSV, verifies their exchange credentials,
# and stores them encrypted across `participants` and `participant_api_keys`.

load_dotenv()

logger = logging.getLogger(__name__)

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

SIGNUPS_CSV = "participant_signups.csv"

# Same registry as post_to_supabase.VENUES. Kept here too rather than imported
# because importing that module would run its environment check (it demands
# FERNET_KEY and SUPABASE_*) and build a second Supabase client as a side
# effect of registering someone.
VENUES = {
    'coinbase': coinbase,
    'lighter': lighter,
}


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


def _verify_lighter(row, account_type: str) -> dict:
    """
    Verify a Lighter credential and resolve its account index.

    Lighter differs from Coinbase in three ways that all have to be caught
    here rather than at sync time, when the participant is no longer around
    to fix anything:

      - it is PERPS ONLY, so a row registered as 'spot' is a mistake
      - the whole credential is one value in api_key; there is no secret
      - the account index lives inside a read-only token, so resolving it
        costs no API call at all

    The live call at the end is what proves the credential actually works.
    """
    if account_type != lighter.ACCOUNT_TYPE:
        raise ValueError(
            f"Lighter has no '{account_type}' market - register this "
            f"credential as '{lighter.ACCOUNT_TYPE}'"
        )

    # Plaintext here; encrypted further down, once proven to work.
    exchange = lighter.build_exchange(_clean(row["api_key"]), encrypted=False)

    account_index = lighter.find_account_index(exchange)
    if account_index is None:
        raise ValueError(
            "No Lighter account for this credential - the wallet may not have "
            "deposited yet"
        )

    # Proves authentication, not just that the token parses.
    exchange.fetch_balance()

    # Standard accounts trade free; Premium accounts pay maker/taker fees that
    # appear NOWHERE in the fill or market data. Their gross performance would
    # be overstated against Coinbase traders, whose fees are recorded - so flag
    # it while someone is watching.
    try:
        fees = lighter.get_account_fee_tier(exchange, account_index)
        if not fees.get('fees_are_zero'):
            logger.warning(
                "Lighter account %s is tier '%s' (taker tick %s, maker tick "
                "%s) - it pays fees that Lighter does not report per trade, "
                "so this participant's costs will be missing from their "
                "returns", account_index, fees.get('user_tier'),
                fees.get('taker_fee_tick'), fees.get('maker_fee_tick'),
            )
    except Exception as e:
        # Informational only - never block a registration over it.
        logger.warning("Could not read Lighter fee tier for %s: %s",
                       account_index, e)

    return {
        'account_type': lighter.ACCOUNT_TYPE,
        # Reused as the venue's account handle, like Coinbase's portfolio
        # UUID. The column is text; the index is an int.
        'portfolio_uuid': str(account_index),
        'passphrase': None,
    }


def verify_and_describe(row) -> dict:
    """
    Check the credentials actually work before storing them, and resolve the
    venue's account handle while we have a live connection.

    Doing this at signup means a participant with bad keys finds out while
    they can still fix it, rather than silently missing from the leaderboard.
    Raises if the credentials don't authenticate.
    """
    account_type = (_clean(row.get("account_type")) or "spot").lower()
    passphrase = _clean(row.get("api_passphrase"))
    exchange_id = _clean(row.get("exchange")) or "coinbase"

    if exchange_id not in VENUES:
        raise ValueError(
            f"No adapter for exchange '{exchange_id}' - known venues are "
            f"{sorted(VENUES)}"
        )

    if exchange_id == 'lighter':
        return _verify_lighter(row, account_type)

    # Plaintext at this point - they're encrypted further down, after they've
    # been proven to work.
    exchange = coinbase.build_exchange(
        _clean(row["api_key"]),
        _clean(row.get("api_secret")),
        exchange_id,
        encrypted=False,
        passphrase=passphrase,
    )

    portfolio_uuid = None
    if account_type == "perp":
        # Costs one API call, run once here rather than on every sync.
        portfolio_uuid = coinbase.find_perp_portfolio_uuid(exchange)
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
