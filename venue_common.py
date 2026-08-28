"""
Things every venue adapter needs, and no venue owns.

These lived in coinbase.py until there were two exchanges, at which point
lighter.py had to import four helpers - three of them private - from a
sibling adapter. That made Coinbase a dependency of a Lighter-only sync and
implied a hierarchy between peers that doesn't exist.

Nothing here is Coinbase-specific. The cipher, the stablecoin set and the
competition window are properties of the COMPETITION; every adapter reads
them, none of them defines them.
"""

import logging
import os

from cryptography.fernet import Fernet, MultiFernet
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Competition-wide constants
# ---------------------------------------------------------------------------

# Stablecoins and fiat-pegged currencies treated as 1:1 with USD.
#
# Defined ONCE. These were briefly re-declared inside two Coinbase functions,
# which shadowed the module-level set: adding a stablecoin changed how a
# historical transfer was valued but not how a live balance was, so the same
# coin could be worth $1 in one calculation and a ticker lookup in another.
USD_EQUIVALENTS = {
    'USDC', 'USDT', 'USD', 'BUSD', 'DAI', 'TUSD', 'USDP', 'GUSD',
    'FDUSD', 'USDD', 'FRAX', 'LUSD', 'SUSD', 'USDN', 'USDJ', 'MAMUSD',
}

# Preferred quote currencies to try, in order.
QUOTE_PRIORITY = ['USDC', 'USDT', 'USD', 'BUSD', 'FDUSD']

# When the competition starts. Everything that reads history - orders, trades,
# cash flows, on every venue - falls back to this when no `since` is given, so
# it decides what counts as in-competition activity.
#
# One place, not one per venue: with the date inlined per adapter, moving the
# start and missing one would give you Lighter flows from a different window
# than Coinbase orders, and the returns computed from them would be quietly
# wrong.
#
# Override with COMPETITION_START in the environment to change it without a
# redeploy - e.g. COMPETITION_START=2020-01-01T00:00:00Z to pull full history.
COMPETITION_START = os.getenv("COMPETITION_START", "2026-01-01T00:00:00Z")


# ---------------------------------------------------------------------------
# Credential encryption
# ---------------------------------------------------------------------------

_fernet: MultiFernet | None = None


def get_fernet() -> MultiFernet:
    """
    The cipher that protects stored credentials, built lazily so importing an
    adapter doesn't require FERNET_KEY - only the paths that actually decrypt
    do.

    Returns a MultiFernet, not a plain Fernet, so keys can be ROTATED:

      FERNET_KEY           the current key. Everything is encrypted with this.
      FERNET_KEYS_RETIRED  comma-separated older keys, decrypt-only.

    MultiFernet encrypts with the first key and decrypts with any of them, so
    a rotation doesn't strand existing rows. Without this, changing FERNET_KEY
    orphans every stored credential at once and every participant has to issue
    new exchange credentials - which is why a leaked key would otherwise be
    unrecoverable rather than merely urgent.

    Rotation: put the new key in FERNET_KEY, move the old one to
    FERNET_KEYS_RETIRED, run rotate_credentials.py, then drop the retired
    entry once it reports everything re-encrypted.
    """
    global _fernet
    if _fernet is None:
        key = os.getenv("FERNET_KEY")
        if not key:
            raise RuntimeError(
                "FERNET_KEY is not set - cannot decrypt participant credentials"
            )

        keys = [Fernet(key.strip().encode())]
        retired = os.getenv("FERNET_KEYS_RETIRED", "")
        keys.extend(
            Fernet(k.strip().encode()) for k in retired.split(",") if k.strip()
        )

        _fernet = MultiFernet(keys)
    return _fernet


# ---------------------------------------------------------------------------
# Shapes exchanges disagree about
# ---------------------------------------------------------------------------

def money_amount(field, default: float = 0.0) -> float:
    """
    Unwrap a money field that may be bare or wrapped.

    Exchanges are inconsistent about this even within one response: Coinbase
    INTX returns amounts either bare ("collateral": "12.0897") or wrapped
    ({"value": "12.0897", "currency": "USDC"}). Both shapes are handled, and
    anything unparseable becomes `default` rather than raising - a single odd
    field shouldn't cost a participant their whole balance snapshot.
    """
    if field is None:
        return default
    if isinstance(field, dict):
        field = field.get('value')
    try:
        return float(field)
    except (TypeError, ValueError):
        return default


def resolve_since(exchange, since) -> int:
    """
    Normalise `since` to milliseconds.

    Accepts a millisecond int (ccxt's convention, what the sync passes), an
    ISO8601 string (convenient by hand), or None for COMPETITION_START.

    `exchange` is any ccxt instance - only its parse8601 is used, which is a
    static helper rather than anything venue-specific.
    """
    if since is None:
        since = exchange.parse8601(COMPETITION_START)
    elif isinstance(since, str):
        since = exchange.parse8601(since)

    if since is None:
        raise ValueError(
            "since could not be resolved to a valid timestamp - pass "
            "milliseconds (int) or a parseable ISO8601 string like "
            "'2026-05-01T00:00:00Z'"
        )
    return since
