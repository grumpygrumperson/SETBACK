"""
Tests for the venue registry and the adapter contract.

Participants run strategies ACROSS exchanges - cross-exchange arbitrage means
one position is split between Coinbase and Lighter. A venue that registers
but can't sync, or syncs but can't register, doesn't merely lose that venue's
data: it leaves the other leg looking like an unhedged directional bet. These
tests are what stops a half-written adapter reaching production.
"""

import threading
import time

import pytest

venues = pytest.importorskip("venues")
venue_common = pytest.importorskip("venue_common")


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exchange_id", sorted(venues.VENUES))
def test_every_registered_venue_implements_the_contract(exchange_id):
    """
    The check that earns this module's existence. A third adapter missing
    get_cash_flows would otherwise fail at 3am, partway through a run, with
    a participant's history already half-written.
    """
    missing = venues.missing_functions(venues.VENUES[exchange_id])
    assert not missing, f"{exchange_id} is missing {missing}"


@pytest.mark.parametrize("exchange_id", sorted(venues.VENUES))
def test_every_venue_declares_its_ccxt_id(exchange_id):
    """The registry key must match what the adapter calls itself."""
    venue = venues.VENUES[exchange_id]
    assert getattr(venue, "EXCHANGE_ID", exchange_id) == exchange_id


def test_missing_functions_detects_an_incomplete_adapter():
    class HalfWritten:
        build_from_credential = staticmethod(lambda c: None)
        # everything else absent

    missing = venues.missing_functions(HalfWritten)
    assert "get_cash_flows" in missing
    assert "build_from_credential" not in missing


def test_missing_functions_rejects_non_callables():
    """A name that exists but isn't callable is not an implementation."""
    class NotCallable:
        get_cash_flows = "surprise"

    assert "get_cash_flows" in venues.missing_functions(NotCallable)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def test_known_venue_resolves():
    assert venues.get("coinbase").EXCHANGE_ID == "coinbase"
    assert venues.get("lighter").EXCHANGE_ID == "lighter"


def test_unknown_venue_raises_rather_than_returning_none():
    """
    Silently skipping an unsupported exchange would drop a participant from
    the leaderboard while every run still reported success.
    """
    with pytest.raises(venues.UnknownVenue, match="binance"):
        venues.get("binance")


def test_unknown_venue_error_lists_what_is_supported():
    with pytest.raises(venues.UnknownVenue, match="coinbase"):
        venues.get("kraken")


def test_unknown_venue_is_a_valueerror():
    """Callers that catch ValueError shouldn't have to know this subclass."""
    assert issubclass(venues.UnknownVenue, ValueError)


# ---------------------------------------------------------------------------
# Only one registry
# ---------------------------------------------------------------------------

def test_callers_share_the_single_registry():
    """
    post_to_supabase and sign_ups each had their own VENUES dict, free to
    drift. Both now go through this module; neither should define its own.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("post_to_supabase.py", "sign_ups.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        assigned = {
            t.id
            for node in tree.body if isinstance(node, ast.Assign)
            for t in node.targets if isinstance(t, ast.Name)
        }
        assert "VENUES" not in assigned, f"{name} defines its own registry again"


# ---------------------------------------------------------------------------
# venue_common holds nothing venue-specific
# ---------------------------------------------------------------------------

def test_adapters_do_not_import_each_other():
    """
    lighter.py used to import four helpers from coinbase.py, three of them
    private - making Coinbase a dependency of a Lighter-only sync and
    implying a hierarchy between peers that doesn't exist.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name, forbidden in (("lighter.py", "coinbase"), ("coinbase.py", "lighter")):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != forbidden, f"{name} imports from {forbidden}"
            elif isinstance(node, ast.Import):
                assert forbidden not in [a.name for a in node.names], \
                    f"{name} imports {forbidden}"


def test_shared_constants_have_one_definition():
    """
    USD_EQUIVALENTS was once re-declared inside two Coinbase functions,
    shadowing the module set: adding a stablecoin changed how a transfer was
    valued but not how a balance was.
    """
    import coinbase
    import lighter

    assert coinbase.USD_EQUIVALENTS is venue_common.USD_EQUIVALENTS
    assert lighter.USD_EQUIVALENTS is venue_common.USD_EQUIVALENTS


def test_competition_start_is_shared():
    """
    One date, not one per venue - otherwise moving the start and missing an
    adapter gives Lighter flows from a different window than Coinbase orders.
    """
    import coinbase
    assert coinbase.COMPETITION_START == venue_common.COMPETITION_START


# ---------------------------------------------------------------------------
# The database agrees with the registry
# ---------------------------------------------------------------------------

def _schema_exchange_checks() -> dict:
    """Every `check (exchange in (...))` in schema.sql, by constraint name."""
    import re
    from pathlib import Path

    sql = (Path(__file__).resolve().parent.parent
           / "migrations" / "schema.sql").read_text(encoding="utf-8")

    found = {}
    for name, values in re.findall(
        r"add constraint (\w*exchange_check)\s+check \(exchange in \(([^)]*)\)\)",
        sql,
    ):
        found[name] = {v.strip().strip("'") for v in values.split(",")}
    return found


def test_schema_constrains_exchange_on_every_table():
    """
    An unconstrained `exchange` column lets a typo at registration insert
    happily and then fail every sync with UnknownVenue - the participant is
    silently absent for as long as nobody reads fetch_errors.
    """
    checks = _schema_exchange_checks()
    for table in ("participant_api_keys", "trade_metrics",
                  "balance_snapshots", "cash_flows"):
        assert f"{table}_exchange_check" in checks, \
            f"{table}.exchange has no check constraint"


@pytest.mark.parametrize("constraint", sorted(_schema_exchange_checks()))
def test_schema_venue_list_matches_the_registry(constraint):
    """
    The cost of the constraints above is that a third venue needs a line in
    schema.sql as well as in venues.py. This is what stops the two drifting:
    a venue in the registry but not the database would be rejected on insert,
    and one in the database but not the registry would be accepted and then
    never sync.
    """
    assert _schema_exchange_checks()[constraint] == set(venues.VENUES), (
        f"{constraint} allows {sorted(_schema_exchange_checks()[constraint])} "
        f"but venues.VENUES has {sorted(venues.VENUES)}"
    )


# ---------------------------------------------------------------------------
# Shared market and price caches
#
# Reference data is identical for every participant. Re-fetching it per
# credential was the largest avoidable cost in a sync.
# ---------------------------------------------------------------------------

class _FakeExchange:
    """Counts how often it would hit the network."""

    def __init__(self, exchange_id="fake", tickers=None, has_bulk=True):
        self.id = exchange_id
        self.markets = None
        self.currencies = None
        self.load_calls = 0
        self.ticker_calls = 0
        self._tickers = tickers if tickers is not None else {"BTC/USD": {"last": 1.0}}
        self.has = {"fetchTickers": has_bulk}
        self._now = 1_700_000_000_000

    def load_markets(self):
        self.load_calls += 1
        self.markets = {"BTC/USD": {}}
        self.currencies = {"BTC": {}}
        return self.markets

    def set_markets(self, markets, currencies=None):
        self.markets = markets
        self.currencies = currencies

    def fetch_tickers(self):
        self.ticker_calls += 1
        return self._tickers

    def milliseconds(self):
        return self._now


@pytest.fixture(autouse=True)
def _clear_caches():
    venue_common.clear_shared_markets()
    venue_common.clear_shared_tickers()
    yield
    venue_common.clear_shared_markets()
    venue_common.clear_shared_tickers()


def test_markets_are_fetched_once_per_exchange():
    a, b = _FakeExchange(), _FakeExchange()
    venue_common.load_shared_markets(a)
    venue_common.load_shared_markets(b)
    assert a.load_calls == 1
    assert b.load_calls == 0, "second credential must reuse the cached markets"
    assert b.markets == a.markets


def test_different_exchanges_do_not_share_markets():
    cb, li = _FakeExchange("coinbase"), _FakeExchange("lighter")
    venue_common.load_shared_markets(cb)
    venue_common.load_shared_markets(li)
    assert cb.load_calls == 1 and li.load_calls == 1


def test_clearing_markets_forces_a_refetch():
    a = _FakeExchange()
    venue_common.load_shared_markets(a)
    venue_common.clear_shared_markets("fake")
    venue_common.load_shared_markets(a)
    assert a.load_calls == 2


def test_tickers_are_fetched_once_per_exchange():
    """
    Also a fairness property: every participant is priced from the same
    snapshot, rather than the first at 12:00 and the last eight minutes later.
    """
    a, b = _FakeExchange(), _FakeExchange()
    first = venue_common.load_shared_tickers(a)
    second = venue_common.load_shared_tickers(b)
    assert a.ticker_calls == 1
    assert b.ticker_calls == 0
    assert first == second


def test_stale_ticker_snapshot_is_refetched():
    a = _FakeExchange()
    venue_common.load_shared_tickers(a, max_age_ms=1000)
    a._now += 5000
    venue_common.load_shared_tickers(a, max_age_ms=1000)
    assert a.ticker_calls == 2


def test_exchange_without_bulk_tickers_returns_empty():
    """Callers fall back to per-symbol lookups rather than losing prices."""
    a = _FakeExchange(has_bulk=False)
    assert venue_common.load_shared_tickers(a) == {}
    assert a.ticker_calls == 0


def test_failed_bulk_fetch_degrades_instead_of_raising():
    class Broken(_FakeExchange):
        def fetch_tickers(self):
            raise RuntimeError("upstream down")

    assert venue_common.load_shared_tickers(Broken()) == {}


# ---------------------------------------------------------------------------
# Cash-flow scope is part of the contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("exchange_id", sorted(venues.VENUES))
def test_every_venue_declares_its_cash_flow_scope(exchange_id):
    """
    The sync asks each adapter whether one credential's transfer history
    covers the whole account. Leaving it undeclared defaults to per-account,
    which is the SAFE direction - flows get fetched more often than needed
    rather than missed - but a venue that is actually account-wide and stays
    silent stores every deposit once per credential, and the metrics layer
    subtracts all of them.
    """
    venue = venues.VENUES[exchange_id]
    assert isinstance(getattr(venue, "CASH_FLOWS_ARE_ACCOUNT_WIDE", None), bool), \
        f"{exchange_id} does not declare CASH_FLOWS_ARE_ACCOUNT_WIDE"


# ---------------------------------------------------------------------------
# The caches are safe to share across threads
#
# The sync fetches credentials concurrently, so several threads reach a cold
# cache at the same instant.
# ---------------------------------------------------------------------------

class _SlowExchange(_FakeExchange):
    """
    A fake whose network calls actually take a moment.

    The delay is the whole point. With an instant fake the interpreter rarely
    switches threads mid-call, so an unlocked cache passes by luck and the
    test proves nothing - which is exactly what the first version of these
    two tests did. A real load_markets() on Coinbase takes ~2s, and that is
    the window several threads pile into.
    """

    def __init__(self, delay=0.05, **kw):
        super().__init__(**kw)
        self._delay = delay

    def load_markets(self):
        time.sleep(self._delay)
        return super().load_markets()

    def fetch_tickers(self):
        time.sleep(self._delay)
        return super().fetch_tickers()


def _run_together(fn, exchanges):
    """Call fn(exchange) on every exchange at the same instant."""
    start = threading.Barrier(len(exchanges))
    results = {}

    def go(ex):
        start.wait()
        results[id(ex)] = fn(ex)

    threads = [threading.Thread(target=go, args=(ex,)) for ex in exchanges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_credentials_load_markets_once():
    """
    Without the lock every thread misses, every thread calls load_markets(),
    and the shared cache saves nothing on the one run where it matters most:
    the first.
    """
    exchanges = [_SlowExchange() for _ in range(6)]
    _run_together(venue_common.load_shared_markets, exchanges)

    assert sum(ex.load_calls for ex in exchanges) == 1
    assert all(ex.markets for ex in exchanges)


def test_concurrent_credentials_take_one_price_snapshot():
    """
    Also the fairness property under concurrency: every participant in the run
    is priced from the same snapshot, not from whichever thread's fetch landed
    first.
    """
    exchanges = [_SlowExchange() for _ in range(6)]
    seen = list(_run_together(venue_common.load_shared_tickers, exchanges).values())

    assert sum(ex.ticker_calls for ex in exchanges) == 1
    assert all(s is seen[0] for s in seen)


# ---------------------------------------------------------------------------
# The documentation agrees with the code
#
# Same principle as the schema check above: an artifact that is not Python
# cannot be kept correct by review alone. .env.example is the only inventory
# of what the service needs to run, and five of the variables live inside
# ENV_CREDENTIALS dicts rather than in os.getenv() calls, so they are
# invisible to anyone grepping for the obvious pattern.
#
# Drift here is not cosmetic. An undocumented variable is one an operator
# does not set on Railway, and the failure modes are silent: no
# COMPETITION_END means the leaderboard keeps moving after the competition
# closes.
# ---------------------------------------------------------------------------

def _env_vars_read_by_code() -> set[str]:
    """Every environment variable name the service reads."""
    import ast
    import re
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    out = subprocess.run(["git", "ls-files", "*.py"], cwd=root,
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        return set()

    names: set[str] = set()
    for rel in out.stdout.split():
        if rel.startswith("tests/"):
            continue
        source = (root / rel).read_text(encoding="utf-8")

        names |= set(re.findall(r'os\.getenv\(\s*"([A-Z][A-Z0-9_]*)"', source))
        names |= set(re.findall(
            r'os\.environ(?:\.get)?\(?\[?\s*"([A-Z][A-Z0-9_]*)"', source))

        # ENV_CREDENTIALS maps an account_type to the NAME of the variable
        # holding that venue's credential, so the name is a dict value and
        # never appears next to os.getenv.
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "ENV_CREDENTIALS"
                    for t in node.targets):
                for leaf in ast.walk(node.value):
                    if (isinstance(leaf, ast.Constant)
                            and isinstance(leaf.value, str)
                            and re.fullmatch(r"[A-Z][A-Z0-9_]*", leaf.value)):
                        names.add(leaf.value)

    return names


def _env_vars_documented() -> set[str]:
    """Every variable named in .env.example, commented-out ones included."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent
            / ".env.example").read_text(encoding="utf-8")
    return set(re.findall(r'^#?\s*([A-Z][A-Z0-9_]*)=', text, re.M))


def test_every_env_var_is_documented():
    """A variable the code reads but nobody knows to set."""
    read = _env_vars_read_by_code()
    if not read:
        import pytest
        pytest.skip("git unavailable - cannot enumerate the tracked sources")

    undocumented = read - _env_vars_documented()
    assert not undocumented, (
        f"read by the code but absent from .env.example: "
        f"{sorted(undocumented)}"
    )


def test_no_documented_env_var_is_dead():
    """
    The mirror. A documented variable nothing reads is worse than useless:
    an operator sets it, believes it took effect, and it never did.
    """
    read = _env_vars_read_by_code()
    if not read:
        import pytest
        pytest.skip("git unavailable - cannot enumerate the tracked sources")

    dead = _env_vars_documented() - read
    assert not dead, (
        f"documented in .env.example but read by nothing: {sorted(dead)}"
    )
