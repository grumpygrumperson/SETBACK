"""
The venue registry, and the contract every adapter has to satisfy.

Participants run strategies ACROSS exchanges - cross-exchange arbitrage means
one person's position is split between Coinbase and Lighter, and money moves
between them constantly. So the two venues are never independent: a leg
missing from one side doesn't just lose that leg, it makes the other side
look like an unhedged directional bet, and a transfer seen on only one side
reads as a real deposit. Every venue has to report, or the picture is worse
than incomplete - it's misleading.

That's why this module exists rather than a dict in each caller. There used
to be one registry in post_to_supabase and another in sign_ups, free to
drift; a venue registered but not syncable, or syncable but not registerable,
would have been silent in both directions.
"""

import coinbase
import lighter

# ---------------------------------------------------------------------------
# The registry
#
# Keyed by ccxt exchange id, which is also what participant_api_keys.exchange
# stores and what gets stamped on every row a credential produces. Adding a
# third exchange means writing an adapter and adding one line here.
# ---------------------------------------------------------------------------

VENUES = {
    'coinbase': coinbase,
    'lighter': lighter,
}


# What the sync and the signup importer call on an adapter. Names only - the
# signatures are documented on each adapter's own functions.
#
#   build_from_credential(credential)          -> ccxt.Exchange
#   verify_credential(row)                     -> dict, at signup
#   get_account_totals_usdc(ex, account_type=, portfolio_uuid=)
#   closed_orders(ex, since=, portfolio_uuid=) -> list[dict]
#   get_cash_flows(ex, since=, portfolio_uuid=) -> list[dict]
#   account_type_from_order(order)             -> str
REQUIRED_FUNCTIONS = (
    'build_from_credential',
    'verify_credential',
    'get_account_totals_usdc',
    'closed_orders',
    'get_cash_flows',
    'account_type_from_order',
)


class UnknownVenue(ValueError):
    """Raised for an exchange id no adapter handles."""


def get(exchange_id: str):
    """
    The adapter for one exchange id.

    Raises UnknownVenue rather than returning None so a credential registered
    against an unsupported exchange fails loudly. Silently skipping it would
    drop a participant off the leaderboard while every run still reported
    success - and on a cross-exchange strategy, dropping one venue leaves the
    other looking like a naked position rather than a hedge.
    """
    venue = VENUES.get(exchange_id)
    if venue is None:
        raise UnknownVenue(
            f"No adapter for exchange '{exchange_id}' - known venues are "
            f"{sorted(VENUES)}"
        )
    return venue


def missing_functions(venue) -> list[str]:
    """
    Which contract functions an adapter fails to provide.

    Empty for a complete adapter. Used by the test suite so a half-written
    third venue fails at `pytest` rather than at 3am, partway through a run,
    with a participant's history already half-written.
    """
    return [name for name in REQUIRED_FUNCTIONS if not callable(getattr(venue, name, None))]
