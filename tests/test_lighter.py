"""
Tests for lighter.py.

All offline - nothing here touches Lighter's API. The one place the live API
was genuinely needed (confirming that an unfunded wallet raises rather than
returning an empty list) is encoded as a fixture below, taken from the real
response.
"""

import pytest

lighter = pytest.importorskip("lighter")
ccxt = pytest.importorskip("ccxt")

# A syntactically valid L1 key for a wallet that owns nothing. Never a real
# key - tests must never depend on one.
FAKE_L1 = "0x" + "a" * 64


# ---------------------------------------------------------------------------
# build_exchange
# ---------------------------------------------------------------------------

def test_builds_from_a_plaintext_key():
    ex = lighter.build_exchange(FAKE_L1, encrypted=False)
    assert ex.id == "lighter"
    assert ex.privateKey == FAKE_L1


def test_account_index_reaches_ccxt_options():
    """Stored at signup so the sync doesn't re-derive it every run."""
    ex = lighter.build_exchange(FAKE_L1, encrypted=False, account_index=1077)
    assert ex.options["accountIndex"] == 1077


def test_api_key_blob_is_rejected_with_an_actionable_message():
    """
    Lighter issues 40-byte (80 hex char) API keys, and ccxt wants the 32-byte
    L1 wallet key. Pasting the wrong one is the likeliest setup mistake, and
    ccxt's own error points at a FAQ rather than saying which key it wants.
    """
    api_key_blob = "b" * 80
    with pytest.raises(ValueError, match="L1 private key"):
        lighter.build_exchange(api_key_blob, encrypted=False)


def test_missing_key_is_rejected():
    with pytest.raises(ValueError, match="required"):
        lighter.build_exchange("", encrypted=False)


def test_encrypted_key_round_trips():
    from coinbase import _get_fernet
    token = _get_fernet().encrypt(FAKE_L1.encode()).decode()
    ex = lighter.build_exchange(token, encrypted=True)
    assert ex.privateKey == FAKE_L1


def test_undecryptable_key_names_the_likely_cause():
    with pytest.raises(ValueError, match="FERNET_KEY"):
        lighter.build_exchange("not-a-fernet-token", encrypted=True)


# ---------------------------------------------------------------------------
# Venue facts
#
# These assert against ccxt itself. If a future ccxt release gives Lighter a
# spot product, or moves it off privateKey auth, this module's assumptions
# break and these tests are what says so.
# ---------------------------------------------------------------------------

def test_ccxt_still_says_lighter_is_perps_only():
    ex = ccxt.lighter()
    assert ex.has["spot"] is False
    assert ex.has["swap"] is True


def test_ccxt_still_wants_a_private_key():
    ex = ccxt.lighter()
    required = {k for k, v in ex.requiredCredentials.items() if v}
    assert required == {"privateKey"}


def test_ccxt_still_has_no_combined_transfer_endpoint():
    """get_cash_flows merges two calls because of this."""
    ex = ccxt.lighter()
    assert not ex.has.get("fetchDepositsWithdrawals")
    assert ex.has["fetchDeposits"] and ex.has["fetchWithdrawals"]


def test_spot_is_refused():
    """
    Returning an empty balance would read as a participant with no money.
    Refusing says the credential was registered wrong.
    """
    ex = lighter.build_exchange(FAKE_L1, encrypted=False)
    with pytest.raises(ValueError, match="no 'spot' account"):
        lighter.get_account_totals_usdc(ex, account_type="spot")


def test_account_type_is_always_perp():
    assert lighter.account_type_from_order({"symbol": "ETH/USDC:USDC"}) == "perp"
    assert lighter.account_type_from_order({}) == "perp"


# ---------------------------------------------------------------------------
# find_account_index
# ---------------------------------------------------------------------------

class _Raises:
    """Stands in for an exchange whose wallet has no Lighter account."""

    def __init__(self, error):
        self._error = error
        self.privateKey = FAKE_L1
        # Private-key path: no accountIndex cached, so find_account_index
        # has to ask the API (and hit the error under test).
        self.options = {}

    def fetch_balance(self, params=None):
        raise self._error

    @staticmethod
    def eth_get_address_from_private_key(_key):
        return "0xdeadbeef"


def test_unfunded_wallet_returns_none_not_an_error():
    """
    Taken from the live API: Lighter reports an unknown wallet as
    code 21100 "account not found", NOT as an empty account list. Registering
    a wallet that hasn't deposited yet must give a usable answer rather than
    an opaque ExchangeError.
    """
    error = ccxt.ExchangeError('lighter {"code":21100,"message":"account not found"}')
    assert lighter.find_account_index(_Raises(error)) is None


def test_other_exchange_errors_still_propagate():
    """Only 'account not found' is benign - everything else is a real fault."""
    error = ccxt.ExchangeError('lighter {"code":500,"message":"internal error"}')
    with pytest.raises(ccxt.ExchangeError):
        lighter.find_account_index(_Raises(error))


# ---------------------------------------------------------------------------
# closed_orders - fills aggregated back into orders
# ---------------------------------------------------------------------------

def fill(order_id, timestamp, price, amount, fee=0.0, symbol="ETH/USDC:USDC",
         side="buy"):
    return {"order_id": order_id, "trade_id": f"t{timestamp}",
            "timestamp": timestamp, "datetime": f"d{timestamp}",
            "symbol": symbol, "type": "limit", "side": side,
            "price": price, "amount": amount,
            "fee_cost": fee, "fee_currency": "USDC"}


@pytest.fixture
def fills(monkeypatch):
    def install(rows):
        monkeypatch.setattr(lighter, "closed_trades", lambda *a, **k: rows)
    return install


def test_single_fill_becomes_one_order(fills):
    fills([fill("A", 1000, 100.0, 2.0, fee=0.5)])
    order = lighter.closed_orders(None)[0]
    assert order["order_id"] == "A"
    assert order["amount"] == 2.0
    assert order["price"] == 100.0
    assert order["fee_cost"] == 0.5


def test_partial_fills_use_a_size_weighted_price(fills):
    """(1 x 100 + 3 x 200) / 4 = 175, not the 150 a plain mean would give."""
    fills([fill("A", 1000, 100.0, 1.0), fill("A", 2000, 200.0, 3.0)])
    order = lighter.closed_orders(None)[0]
    assert order["amount"] == 4.0
    assert order["price"] == pytest.approx(175.0)


def test_fees_accumulate_across_fills(fills):
    fills([fill("A", 1000, 100.0, 1.0, fee=0.1),
           fill("A", 2000, 100.0, 1.0, fee=0.3)])
    assert lighter.closed_orders(None)[0]["fee_cost"] == pytest.approx(0.4)


def test_order_timestamp_is_the_last_fill(fills):
    """An order is closed when it last filled, matching Coinbase semantics."""
    fills([fill("A", 5000, 100.0, 1.0), fill("A", 1000, 100.0, 1.0)])
    assert lighter.closed_orders(None)[0]["timestamp"] == 5000


def test_orders_come_back_chronological(fills):
    fills([fill("A", 3000, 1.0, 1.0), fill("B", 1000, 1.0, 1.0),
           fill("C", 2000, 1.0, 1.0)])
    assert [o["order_id"] for o in lighter.closed_orders(None)] == ["B", "C", "A"]


def test_internal_notional_key_never_reaches_the_database(fills):
    """_notional is scratch space; trade_metrics has no such column."""
    fills([fill("A", 1000, 100.0, 1.0)])
    assert "_notional" not in lighter.closed_orders(None)[0]


def test_rows_match_the_trade_metrics_columns(fills):
    """
    A key with no column makes PostgREST reject the whole upsert, losing
    every order in the batch rather than just the offending field.
    """
    import re
    from pathlib import Path

    sql = Path(__file__).resolve().parent.parent / "migrations" / "schema.sql"
    block = sql.read_text(encoding="utf-8").split(
        "create table if not exists public.trade_metrics")[1].split(");")[0]
    columns = set(re.findall(r"^\s{2}(\w+)", block, re.M))

    fills([fill("A", 1000, 100.0, 1.0)])
    assert set(lighter.closed_orders(None)[0]) <= columns


def test_fill_without_an_order_id_is_skipped_not_fatal(fills):
    orphan = {**fill("X", 2000, 1.0, 1.0), "order_id": None, "trade_id": None}
    fills([fill("A", 1000, 100.0, 1.0), orphan])
    assert [o["order_id"] for o in lighter.closed_orders(None)] == ["A"]


def test_no_fills_gives_no_orders(fills):
    fills([])
    assert lighter.closed_orders(None) == []


def test_orders_are_stamped_for_the_sync_to_fill_in(fills):
    fills([fill("A", 1000, 100.0, 1.0)])
    order = lighter.closed_orders(None)[0]
    assert order["participant_id"] is None      # sync populates
    assert order["account_type"] == "perp"


def test_wallet_address_is_rejected_as_a_key():
    """
    An address is 42 chars, short enough to pass the length check, so ccxt
    would derive a different address from it and Lighter would answer
    "account not found" - indistinguishable from a real but unfunded wallet.
    """
    address = "0x" + "3c03630c49b74c481d8458fa31e05828bf170bc3"
    with pytest.raises(ValueError, match="ADDRESS, not a private key"):
        lighter.build_exchange(address, encrypted=False)


# ---------------------------------------------------------------------------
# Read-only tokens
#
# The preferred credential: unlike a wallet private key it cannot move funds
# and expires on its own, so collecting one per participant carries no
# custody risk.
# ---------------------------------------------------------------------------

import time

SIG = "d" * 64


def token(account_index=741152, scope="all", deadline=None, prefix="ro"):
    deadline = deadline if deadline is not None else int(time.time()) + 86400
    return f"{prefix}:{account_index}:{scope}:{deadline}:{SIG}"


def test_token_fields_are_parsed():
    parsed = lighter.parse_readonly_token(token(account_index=741152))
    assert parsed["account_index"] == 741152
    assert parsed["scope"] == "all"
    assert isinstance(parsed["deadline"], int)


def test_deadline_is_an_int_not_a_string():
    """ccxt compares the deadline numerically before reusing a cached token."""
    parsed = lighter.parse_readonly_token(token())
    assert isinstance(parsed["deadline"], int)


@pytest.mark.parametrize("bad", [
    "",
    "not-a-token",
    "ro:741152:all",                      # too few segments
    "xx:741152:all:9999999999:" + SIG,    # wrong prefix
])
def test_malformed_tokens_are_rejected(bad):
    with pytest.raises(ValueError):
        lighter.parse_readonly_token(bad)


def test_non_numeric_fields_are_rejected():
    with pytest.raises(ValueError, match="numeric"):
        lighter.parse_readonly_token(f"ro:abc:all:9999999999:{SIG}")


def test_token_builds_an_exchange_with_no_private_key():
    """The whole point: authenticate without a credential that can move money."""
    ex = lighter.build_exchange_from_token(token(), encrypted=False)
    assert ex.privateKey is None
    assert ex.options["accountIndex"] == 741152


def test_token_is_cached_where_ccxt_looks_for_it():
    """
    create_auth() reads options['auths'][accountIndex][apiKeyIndex]. If this
    layout ever changes, ccxt falls back to signing and the token path breaks.
    """
    raw = token()
    ex = lighter.build_exchange_from_token(raw, encrypted=False)
    cached = ex.options["auths"]["741152"][str(lighter._DEFAULT_API_KEY_INDEX)]
    assert cached["token"] == raw
    assert cached["signer"] is None


def test_api_key_index_is_within_the_range_ccxt_accepts():
    """ccxt validates apiKeyIndex into 4..254 and rejects 0."""
    assert 4 <= lighter._DEFAULT_API_KEY_INDEX <= 254


def test_expired_token_is_refused_up_front():
    """
    Better than letting Lighter answer 'invalid auth string' mid-sync, which
    gives no hint that the fix is to reissue the token.
    """
    with pytest.raises(ValueError, match="expired"):
        lighter.build_exchange_from_token(
            token(deadline=int(time.time()) - 86400), encrypted=False)


def test_expiring_token_warns(caplog):
    soon = int(time.time()) + 3 * 86400
    with caplog.at_level("WARNING"):
        lighter.build_exchange_from_token(token(deadline=soon), encrypted=False)
    assert "expires in" in caplog.text


def test_build_exchange_routes_a_token_to_the_token_path():
    """
    Both credential types share one column, so the sync must not need to know
    which a participant registered.
    """
    ex = lighter.build_exchange(token(), encrypted=False)
    assert ex.privateKey is None
    assert ex.options["accountIndex"] == 741152


def test_build_exchange_routes_an_encrypted_token_too():
    from coinbase import _get_fernet
    sealed = _get_fernet().encrypt(token().encode()).decode()
    ex = lighter.build_exchange(sealed, encrypted=True)
    assert ex.privateKey is None
    assert ex.options["accountIndex"] == 741152


def test_private_key_still_works_alongside_tokens():
    ex = lighter.build_exchange(FAKE_L1, encrypted=False)
    assert ex.privateKey == FAKE_L1


def test_find_account_index_uses_the_token_without_an_api_call():
    """The index is a field in the token, so signup needs no lookup."""
    ex = lighter.build_exchange_from_token(token(account_index=999), encrypted=False)
    assert lighter.find_account_index(ex) == 999


def test_absent_fee_stays_none_rather_than_becoming_zero(fills):
    """
    Lighter's trade payload has no fee field. Storing 0.0 would assert the
    trade was free when the truth is unknown - and the leaderboard scores
    people on net performance.
    """
    row = fill("A", 1000, 100.0, 1.0)
    row["fee_cost"] = None
    fills([row])
    assert lighter.closed_orders(None)[0]["fee_cost"] is None


def test_partially_reported_fees_sum_what_is_known(fills):
    a = fill("A", 1000, 100.0, 1.0, fee=0.25)
    b = fill("A", 2000, 100.0, 1.0)
    b["fee_cost"] = None
    fills([a, b])
    assert lighter.closed_orders(None)[0]["fee_cost"] == pytest.approx(0.25)
