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
import threading

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

# When the competition ENDS. Unset means open-ended, which is the right
# default for development and the wrong one for a competition with a prize.
#
# This bounds SCORING, not fetching. The distinction matters:
#
#   fetching   is bounded below by COMPETITION_START so the sync doesn't
#              re-walk years of history, and is deliberately NOT bounded
#              above - the cron keeps recording, which is what you want for
#              an audit trail and for spotting a key that dies after the
#              close.
#
#   scoring    is bounded at BOTH ends, because a leaderboard that keeps
#              moving after the competition closes has no final result. With
#              no end date, day 57's market movement silently rewrites the
#              standings of a contest that already finished - and every
#              participant's rank keeps drifting for as long as the cron
#              runs.
#
# Set it to the last instant that counts, e.g.
# COMPETITION_END=2026-10-27T00:00:00Z for a 56-day run from 2026-09-01.
COMPETITION_END = os.getenv("COMPETITION_END") or None


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


# ---------------------------------------------------------------------------
# Market data sharing
# ---------------------------------------------------------------------------

# Loaded market maps, keyed by ccxt exchange id. Markets are PUBLIC reference
# data - the same product list for every participant - so they're fetched once
# per process rather than once per credential.
_markets_cache: dict = {}

# The sync fetches credentials concurrently, so several threads reach a cold
# cache at the same instant. Without this lock they all miss, all call
# load_markets(), and the shared cache saves nothing on the very run where it
# matters most - the first one. The lock is held across the fetch on purpose:
# the losers wait ~2s once and then read the cache, instead of paying for
# their own download.
_cache_lock = threading.Lock()


def load_shared_markets(exchange) -> None:
    """
    Give this instance the market map, reusing one already loaded for the
    same exchange rather than fetching it again.

    load_markets() costs 7 HTTP requests and ~2.3s on Coinbase (currencies,
    crypto currencies, exchange rates, three pages of products, and the fee
    summary). Paid once per credential that is 200 participants x 7 = 1,400
    redundant requests per run, and roughly eight minutes of a fifteen-minute
    cron spent re-downloading an identical product list.

    Safe to share because markets are not account-scoped. The one
    account-specific thing load_markets() picks up on Coinbase is the fee
    tier it writes into market['taker']/['maker'] - and nothing here reads
    those: Coinbase fees come from each order's own total_fees, and Lighter's
    come from get_account_fee_tier(), which asks a private per-account
    endpoint precisely because the market-level rate is only the Standard
    default.

    Cleared by clear_shared_markets(); a long-lived process should call that
    periodically so a newly listed market eventually appears.
    """
    with _cache_lock:
        cached = _markets_cache.get(exchange.id)
        if cached is None:
            exchange.load_markets()
            _markets_cache[exchange.id] = (exchange.markets, exchange.currencies)
            return

        markets, currencies = cached

    exchange.set_markets(markets, currencies)


# Bulk ticker snapshots, keyed by ccxt exchange id: (fetched_at_ms, tickers).
_tickers_cache: dict = {}

# How long a price snapshot stays usable. Long enough that one sync run makes
# a single call, short enough that a long-running process re-prices.
_TICKER_TTL_MS = 5 * 60 * 1000


def load_shared_tickers(exchange, max_age_ms: int = _TICKER_TTL_MS) -> dict:
    """
    One bulk price snapshot per exchange, shared by every participant.

    Pricing a balance coin by coin means one fetch_ticker request each -
    around 30 sequential HTTP calls for a participant holding 30 assets, and
    it was the single largest cost in a sync. fetch_tickers() returns all 929
    Coinbase symbols in one ~2s request, so the whole run costs one call
    instead of thirty per credential.

    It is also FAIRER, which matters more than the speed. Valuing each
    participant with its own ticker calls means the first is priced at
    12:00:01 and the last at 12:08:30 - eight minutes of market movement
    separating people who are supposed to be ranked against each other. A
    shared snapshot values everyone at the same instant.

    Returns {} rather than raising if the bulk call fails, so callers fall
    back to per-symbol lookups instead of losing the snapshot entirely.
    """
    if not exchange.has.get('fetchTickers'):
        return {}

    # Held across the fetch so a cold cache costs ONE bulk call however many
    # threads arrive at once - and, just as importantly, so every participant
    # in the run is priced from that single snapshot rather than from
    # whichever concurrent fetch happened to land first.
    with _cache_lock:
        now = exchange.milliseconds()
        cached = _tickers_cache.get(exchange.id)
        if cached is not None:
            fetched_at, tickers = cached
            if now - fetched_at < max_age_ms:
                return tickers

        try:
            tickers = exchange.fetch_tickers()
        except Exception as e:
            logger.warning("Bulk ticker fetch failed on %s, falling back to "
                           "per-symbol pricing: %s", exchange.id, e)
            return {}

        _tickers_cache[exchange.id] = (now, tickers)
        return tickers


def clear_shared_tickers(exchange_id: str = None) -> None:
    """Forget cached prices, for one exchange or all of them."""
    with _cache_lock:
        if exchange_id is None:
            _tickers_cache.clear()
        else:
            _tickers_cache.pop(exchange_id, None)


def clear_shared_markets(exchange_id: str = None) -> None:
    """
    Forget cached markets, for one exchange or all of them.

    The sync is a short-lived process - it exits between cron runs, so the
    cache never goes stale there. This exists for anything long-running, and
    for tests.
    """
    with _cache_lock:
        if exchange_id is None:
            _markets_cache.clear()
        else:
            _markets_cache.pop(exchange_id, None)


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
