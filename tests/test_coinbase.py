"""
Tests for the pure helpers in coinbase.py.

Nothing here touches an exchange or the network - these are the functions that
interpret what Coinbase sends back, which is exactly where the venue
misclassification bug lived.
"""

import pytest

coinbase = pytest.importorskip("coinbase")
venue_common = pytest.importorskip("venue_common")


# ---------------------------------------------------------------------------
# account_type_from_order
# ---------------------------------------------------------------------------

def order(product_type=None, symbol=None, expiry_type=None) -> dict:
    return {
        "symbol": symbol,
        "info": {"product_type": product_type, "contract_expiry_type": expiry_type},
    }


def test_spot_order():
    assert coinbase.account_type_from_order(order("SPOT", "BTC/USDC")) == "spot"


def test_intx_perp_reported_as_future_is_classified_perp():
    """
    The bug this function was rewritten for. Coinbase reports INTX perpetuals
    as product_type='FUTURE' with no contract_expiry_type - identical to a
    dated future by those fields alone. Storing them as 'future' made the
    resume-point lookup find nothing and refetch all history every run.
    """
    assert coinbase.account_type_from_order(order("FUTURE", "PUMP/USDC:USDC")) == "perp"


def test_dated_future_stays_future():
    """The settle suffix carries an expiry date, so it is not a perpetual."""
    assert coinbase.account_type_from_order(
        order("FUTURE", "BTC/USD:USD-260327")) == "future"


def test_explicit_perpetual_expiry_type_wins():
    assert coinbase.account_type_from_order(
        order("FUTURE", "BTC/USD:USD", expiry_type="PERPETUAL")) == "perp"


def test_product_type_perpetual():
    assert coinbase.account_type_from_order(order("PERPETUAL", "BTC/USD:USD")) == "perp"


def test_future_without_a_usable_symbol_falls_back_to_future():
    assert coinbase.account_type_from_order(order("FUTURE", "BTCUSD")) == "future"


def test_missing_product_type_returns_none():
    assert coinbase.account_type_from_order(order(None, "BTC/USDC")) is None


def test_missing_info_does_not_raise():
    assert coinbase.account_type_from_order({}) is None


def test_lowercase_product_type_is_handled():
    assert coinbase.account_type_from_order(order("spot", "BTC/USDC")) == "spot"


# ---------------------------------------------------------------------------
# _amount
# ---------------------------------------------------------------------------

def test_amount_unwraps_a_bare_string():
    assert venue_common.money_amount("12.0897") == pytest.approx(12.0897)


def test_amount_unwraps_a_money_object():
    """INTX returns amounts both ways, sometimes in the same response."""
    assert venue_common.money_amount({"value": "12.0897", "currency": "USDC"}) == pytest.approx(12.0897)


def test_amount_of_none_is_the_default():
    assert venue_common.money_amount(None) == 0.0
    assert venue_common.money_amount(None, default=-1.0) == -1.0


def test_amount_of_garbage_is_the_default():
    assert venue_common.money_amount("not a number") == 0.0
    assert venue_common.money_amount({"value": None}) == 0.0


def test_amount_accepts_numbers():
    assert venue_common.money_amount(5) == 5.0
    assert venue_common.money_amount(5.5) == 5.5


# ---------------------------------------------------------------------------
# Transfer classification
#
# These are sets, not logic, but the allowlist is load-bearing: an
# 'advanced_trade_fill' counted as funding would subtract every trade from
# that participant's return and flatten their performance to roughly zero.
# ---------------------------------------------------------------------------

def test_trades_are_not_external_transfers():
    for kind in ("advanced_trade_fill", "buy", "sell", "trade"):
        assert kind in coinbase.INTERNAL_ACTIVITY_TYPES
        assert kind not in coinbase.EXTERNAL_TRANSFER_TYPES


def test_spot_to_perp_moves_are_internal():
    """
    Reported only on the spot leg, so the netting heuristic in metrics can
    never pair them - they have to be excluded here.
    """
    assert "intx_deposit" in coinbase.INTERNAL_ACTIVITY_TYPES
    assert "intx_withdrawal" in coinbase.INTERNAL_ACTIVITY_TYPES


def test_real_funding_is_external():
    for kind in ("send", "fiat_deposit", "fiat_withdrawal"):
        assert kind in coinbase.EXTERNAL_TRANSFER_TYPES


def test_the_two_sets_never_overlap():
    assert not (coinbase.EXTERNAL_TRANSFER_TYPES & coinbase.INTERNAL_ACTIVITY_TYPES)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _paginate
#
# All three history endpoints (orders, trades, transfers) now share this, so
# a truncation bug here would silently shorten every participant's history.
# ---------------------------------------------------------------------------

def pages(*batches):
    """A fetch_page callable that serves the given batches in order."""
    served = list(batches)

    def fetch(cursor, limit):
        return served.pop(0) if served else []
    return fetch


def entry(entry_id, timestamp):
    return {"id": entry_id, "timestamp": timestamp}


def test_paginate_stops_on_a_short_page():
    fetch = pages([entry("a", 1), entry("b", 2)])
    assert len(coinbase._paginate(fetch, 0, 10, "test")) == 2


def test_paginate_follows_full_pages():
    fetch = pages(
        [entry("a", 1), entry("b", 2)],
        [entry("c", 3), entry("d", 4)],
        [entry("e", 5)],
    )
    result = coinbase._paginate(fetch, 0, 2, "test")
    assert [e["id"] for e in result] == ["a", "b", "c", "d", "e"]


def test_paginate_advances_past_the_last_timestamp():
    """Without the +1 the boundary entry is refetched on every page."""
    seen_cursors = []

    def fetch(cursor, limit):
        seen_cursors.append(cursor)
        return [entry("a", 100), entry("b", 200)] if len(seen_cursors) == 1 else []

    coinbase._paginate(fetch, 0, 2, "test")
    assert seen_cursors == [0, 201]


def test_paginate_breaks_when_the_exchange_ignores_since():
    """
    An exchange that re-serves the same full page would otherwise loop
    forever. The id guard has to catch it.
    """
    same = [entry("a", 1), entry("b", 2)]

    def fetch(cursor, limit):
        return list(same)

    result = coinbase._paginate(fetch, 0, 2, "test")
    assert [e["id"] for e in result] == ["a", "b"]


def test_paginate_stops_when_a_page_has_no_timestamp():
    fetch = pages(
        [entry("a", 1), entry("b", None)],
        [entry("c", 3)],
    )
    result = coinbase._paginate(fetch, 0, 2, "test")
    assert [e["id"] for e in result] == ["a", "b"]


def test_paginate_handles_entries_without_ids():
    """
    Treating a missing id as a duplicate would break after page one. Endpoints
    that don't set ids must still paginate.
    """
    fetch = pages(
        [{"timestamp": 1}, {"timestamp": 2}],
        [{"timestamp": 3}],
    )
    assert len(coinbase._paginate(fetch, 0, 2, "test")) == 3


def test_paginate_has_a_hard_page_cap():
    """Backstop for a runaway walk where the id guard can't apply."""
    counter = {"n": 0}

    def fetch(cursor, limit):
        counter["n"] += 1
        return [{"timestamp": counter["n"]}, {"timestamp": counter["n"] + 1}]

    coinbase._paginate(fetch, 0, 2, "test")
    assert counter["n"] <= coinbase._MAX_PAGES + 1


def test_paginate_on_empty_history():
    assert coinbase._paginate(pages([]), 0, 10, "test") == []


# ---------------------------------------------------------------------------
# _resolve_since
# ---------------------------------------------------------------------------

class FakeExchange:
    """Only the two ccxt methods _resolve_since touches."""

    @staticmethod
    def parse8601(value):
        import datetime as dt
        try:
            return int(dt.datetime.fromisoformat(
                value.replace("Z", "+00:00")).timestamp() * 1000)
        except (ValueError, AttributeError):
            return None


def test_resolve_since_passes_milliseconds_through():
    assert venue_common.resolve_since(FakeExchange(), 1_700_000_000_000) == 1_700_000_000_000


def test_resolve_since_parses_iso8601():
    assert venue_common.resolve_since(FakeExchange(), "2026-01-01T00:00:00Z") == \
        venue_common.resolve_since(FakeExchange(), None)


def test_resolve_since_defaults_to_competition_start():
    assert venue_common.resolve_since(FakeExchange(), None) is not None


def test_resolve_since_rejects_garbage():
    with pytest.raises(ValueError, match="ISO8601"):
        venue_common.resolve_since(FakeExchange(), "not a date")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_quote_priority_entries_are_all_usd_equivalents():
    """
    Every preferred quote currency must also be treated as $1, or a coin
    priced in it would be converted twice.
    """
    assert set(coinbase.QUOTE_PRIORITY) <= coinbase.USD_EQUIVALENTS


def test_constants_are_not_shadowed_by_local_copies():
    """
    These were once re-declared inside two functions, which meant adding a
    stablecoin changed how transfers were valued but not how balances were.
    """
    import inspect
    for fn in (coinbase.price_balances_in_usdc,
               coinbase.get_account_totals_usdc,
               coinbase._price_at_usdc):
        source = inspect.getsource(fn)
        assert "USD_EQUIVALENTS = {" not in source, f"{fn.__name__} shadows the constant"
        assert "QUOTE_PRIORITY = [" not in source, f"{fn.__name__} shadows the constant"


# ---------------------------------------------------------------------------
# Wallet pagination
#
# ccxt's fetch_balance sends limit=250 and parses a single page - there is no
# cursor loop in it. Coinbase creates a wallet per asset a participant has
# ever held or enabled, so an active account passes 250 and everything after
# that is absent from the balance with nothing to say so.
# ---------------------------------------------------------------------------

class _PagedExchange:
    """Reports a balance, then serves further wallet pages on request."""

    def __init__(self, first_page, pages=()):
        self.id = "coinbase"
        self._first = first_page
        self._pages = list(pages)
        self.page_requests = []

    def fetch_balance(self, params=None):
        return self._first

    def v3PrivateGetBrokerageAccounts(self, request):
        self.page_requests.append(request.get("cursor"))
        return self._pages.pop(0)


def _wallet(currency, available, hold="0"):
    return {"currency": currency,
            "available_balance": {"value": available, "currency": currency},
            "hold": {"value": hold, "currency": currency}}


def test_single_page_balance_costs_no_extra_requests():
    """The common case must stay one request."""
    ex = _PagedExchange({"total": {"BTC": 1.0}, "info": {"has_next": False}})
    assert coinbase._wallet_totals(ex, "spot") == {"BTC": 1.0}
    assert ex.page_requests == []


def test_wallets_past_the_first_page_are_not_lost():
    """
    The silent one. Without this the participant's holdings in wallet 251
    onward are valued at zero on the leaderboard, and it gets worse the more
    assets they trade.
    """
    ex = _PagedExchange(
        {"total": {"BTC": 1.0}, "info": {"has_next": True, "cursor": "c1"}},
        pages=[{"accounts": [_wallet("ETH", "2.5")], "has_next": False}],
    )
    assert coinbase._wallet_totals(ex, "spot") == {"BTC": 1.0, "ETH": 2.5}
    assert ex.page_requests == ["c1"]


def test_held_funds_count_toward_the_total():
    """
    `total` is available + hold. A coin sitting in an open order is still the
    participant's money.
    """
    ex = _PagedExchange(
        {"total": {}, "info": {"has_next": True, "cursor": "c1"}},
        pages=[{"accounts": [_wallet("SOL", "1.5", hold="0.5")], "has_next": False}],
    )
    assert coinbase._wallet_totals(ex, "spot")["SOL"] == 2.0


def test_extra_pages_are_walked_to_the_end():
    ex = _PagedExchange(
        {"total": {}, "info": {"has_next": True, "cursor": "c1"}},
        pages=[
            {"accounts": [_wallet("ETH", "1")], "has_next": True, "cursor": "c2"},
            {"accounts": [_wallet("SOL", "2")], "has_next": False},
        ],
    )
    assert coinbase._wallet_totals(ex, "spot") == {"ETH": 1.0, "SOL": 2.0}
    assert ex.page_requests == ["c1", "c2"]


def test_pagination_stops_at_the_page_cap():
    """
    A cursor that never terminates must not spin forever - the run has a cron
    window to fit inside.
    """
    class Endless(_PagedExchange):
        def v3PrivateGetBrokerageAccounts(self, request):
            self.page_requests.append(request.get("cursor"))
            return {"accounts": [], "has_next": True, "cursor": "again"}

    ex = Endless({"total": {}, "info": {"has_next": True, "cursor": "c1"}})
    coinbase._wallet_totals(ex, "spot")
    assert len(ex.page_requests) < coinbase._MAX_ACCOUNT_PAGES + 1


# ---------------------------------------------------------------------------
# Per-currency transfer lookups
# ---------------------------------------------------------------------------

class _AccountsExchange:
    def __init__(self, accounts=None, raises=False):
        self.id = "coinbase"
        self.accounts = None
        self._accounts = accounts
        self._raises = raises
        self.load_calls = 0

    def load_accounts(self):
        self.load_calls += 1
        if self._raises:
            raise RuntimeError("permission denied")
        self.accounts = self._accounts
        return self.accounts


def test_usable_account_index_loads_once():
    ex = _AccountsExchange(accounts=[{"id": "u", "code": "BTC"}])
    assert coinbase._account_index_is_usable(ex) is True
    assert ex.load_calls == 1


def test_empty_account_list_is_reported_unusable():
    """
    An INTX key sees no v3 brokerage accounts. ccxt's load_accounts only
    caches a NON-empty list, so every per-currency lookup re-downloads the
    whole list before failing to find its wallet - 33 identical requests and
    11.8s on one live perp credential, to produce nothing.
    """
    ex = _AccountsExchange(accounts=[])
    assert coinbase._account_index_is_usable(ex) is False


def test_account_listing_failure_degrades_instead_of_raising():
    ex = _AccountsExchange(raises=True)
    assert coinbase._account_index_is_usable(ex) is False


# ---------------------------------------------------------------------------
# Cash-flow scope
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Read-only enforcement
#
# The competition needs exactly one capability - reading a balance. Every
# capability beyond that is a liability held on the participant's behalf, and
# a key that can withdraw is custody.
#
# Response shape verified live against a real CDP key:
#   {'can_view': True, 'can_trade': False, 'can_transfer': False,
#    'portfolio_uuid': '019ffaf3-...', 'portfolio_type': 'INTX'}
# ---------------------------------------------------------------------------

def _perms(view=True, trade=False, transfer=False, ptype="DEFAULT"):
    return {"can_view": view, "can_trade": trade, "can_transfer": transfer,
            "portfolio_uuid": "uuid-1", "portfolio_type": ptype}


def test_a_view_only_key_passes():
    coinbase.assert_read_only(_perms())


def test_a_key_that_can_withdraw_is_refused():
    """Custody. The worst of the three, so it is checked first."""
    with pytest.raises(ValueError, match="WITHDRAW"):
        coinbase.assert_read_only(_perms(transfer=True))


def test_a_key_that_can_trade_is_refused():
    """
    Even if never used, holding it means the competition COULD trade on a
    participant's account - the accusation you cannot disprove after a
    disputed result.
    """
    with pytest.raises(ValueError, match="TRADE"):
        coinbase.assert_read_only(_perms(trade=True))


def test_a_key_that_cannot_even_read_is_refused():
    with pytest.raises(ValueError, match="cannot read"):
        coinbase.assert_read_only(_perms(view=False))


def test_unknown_permissions_are_refused_not_assumed_safe():
    """
    The one that decides whether this control is real. An empty response means
    the check did not run, and "the check failed so we let it through" is how
    a security control becomes decorative.
    """
    with pytest.raises(ValueError, match="cannot be confirmed read-only"):
        coinbase.assert_read_only({})


def test_withdraw_is_reported_even_when_trade_is_also_set():
    """The more severe capability should be the one named."""
    with pytest.raises(ValueError, match="WITHDRAW"):
        coinbase.assert_read_only(_perms(trade=True, transfer=True))


def test_permissions_are_read_from_a_flat_response():
    class Ex:
        def v3PrivateGetBrokerageKeyPermissions(self):
            return _perms()

    assert coinbase.read_key_permissions(Ex())["can_view"] is True


def test_permissions_are_also_read_from_a_wrapped_response():
    """Defensive: Coinbase has wrapped payloads under 'results' elsewhere."""
    class Ex:
        def v3PrivateGetBrokerageKeyPermissions(self):
            return {"results": _perms(transfer=True)}

    assert coinbase.read_key_permissions(Ex())["can_transfer"] is True


def test_an_empty_response_becomes_an_empty_dict_not_none():
    """So assert_read_only's 'unknown' branch fires instead of a TypeError."""
    class Ex:
        def v3PrivateGetBrokerageKeyPermissions(self):
            return None

    assert coinbase.read_key_permissions(Ex()) == {}


def test_coinbase_declares_its_transfers_account_wide():
    """
    The sync reads this to decide that ONE credential per participant
    collects Coinbase transfers. Flipping it to False would store every
    deposit twice for anyone holding both a spot and a perp key.
    """
    assert coinbase.CASH_FLOWS_ARE_ACCOUNT_WIDE is True
