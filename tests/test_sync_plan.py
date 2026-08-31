"""
Tests for how a run is PLANNED and how one credential is synced.

These cover the two changes that let the sync run credentials concurrently,
and the one that stopped it asking the same venue for the same transfers
twice. Both are the kind of thing that is invisible when it breaks: the run
still reports success, and the damage shows up later as a participant's
returns being quietly wrong.

Nothing here touches the network. The venue adapters and the Supabase writes
are stubbed, because what is being tested is the sync's own bookkeeping.
"""

import pytest

post_to_supabase = pytest.importorskip("post_to_supabase")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Venue:
    """A minimal venue adapter."""

    def __init__(self, account_wide=False, name="fake"):
        self.CASH_FLOWS_ARE_ACCOUNT_WIDE = account_wide
        self.EXCHANGE_ID = name

    def build_from_credential(self, credential):
        return object()


def _credential(exchange="coinbase", account_type="spot", key="ciphertext"):
    return {"exchange": exchange, "account_type": account_type, "api_key": key}


@pytest.fixture
def registry(monkeypatch):
    """Swap the venue registry for stubs, keyed by exchange id."""
    table = {}

    def get(exchange_id):
        if exchange_id not in table:
            raise post_to_supabase.venues.UnknownVenue(exchange_id)
        return table[exchange_id]

    monkeypatch.setattr(post_to_supabase.venues, "get", get)
    monkeypatch.setattr(post_to_supabase, "log_fetch_error",
                        lambda *a, **k: None)
    return table


# ---------------------------------------------------------------------------
# Planning: who collects cash flows
# ---------------------------------------------------------------------------

def test_account_wide_venue_collects_flows_once_per_participant(registry):
    """
    The bug this prevents. Coinbase's transactions endpoint answers for the
    whole ACCOUNT whichever portfolio's key asks, so a participant with a
    spot and a perp credential would store every deposit twice - once under
    each account_type. Nothing rejects it (account_type is in the unique key)
    and mark_internal_transfers only pairs OPPOSITE directions, so two
    identical deposits never cancel: a $1,000 deposit is subtracted from
    their return as $2,000.
    """
    registry["coinbase"] = _Venue(account_wide=True)

    work, skipped = post_to_supabase._plan_credential_work(
        [{"id": "p1"}],
        {"p1": [_credential(account_type="spot"),
                _credential(account_type="perp")]},
    )

    assert [w["sync_flows"] for w in work] == [True, False]
    assert skipped == 0


def test_per_account_venue_collects_flows_for_every_credential(registry):
    """
    The other direction, and just as damaging. Lighter's transfer history is
    scoped to an account index, so skipping the second credential would mean
    its deposits are never seen at all and that participant's returns are
    never adjusted for funding.
    """
    registry["lighter"] = _Venue(account_wide=False, name="lighter")

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}],
        {"p1": [_credential("lighter", "perp"), _credential("lighter", "perp")]},
    )

    assert [w["sync_flows"] for w in work] == [True, True]


def test_flow_claim_does_not_leak_between_participants(registry):
    """
    The claim is per participant. One participant's spot credential must not
    stop a DIFFERENT participant's from collecting their own transfers.
    """
    registry["coinbase"] = _Venue(account_wide=True)

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}, {"id": "p2"}],
        {"p1": [_credential()], "p2": [_credential()]},
    )

    assert [w["sync_flows"] for w in work] == [True, True]


def test_flow_claim_does_not_leak_between_venues(registry):
    """Two venues, two claims - Coinbase's must not consume Lighter's."""
    registry["coinbase"] = _Venue(account_wide=True)
    registry["lighter"] = _Venue(account_wide=False, name="lighter")

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}],
        {"p1": [_credential("coinbase", "spot"), _credential("lighter", "perp")]},
    )

    assert [w["sync_flows"] for w in work] == [True, True]


# ---------------------------------------------------------------------------
# Planning: rows that can't be synced
# ---------------------------------------------------------------------------

def test_row_without_a_key_is_skipped_not_attempted(registry):
    """
    A registration problem, not a sync failure - counting it as failed would
    push a healthy run over the threshold.
    """
    registry["coinbase"] = _Venue()

    work, skipped = post_to_supabase._plan_credential_work(
        [{"id": "p1"}], {"p1": [_credential(key=None)]}
    )

    assert work == []
    assert skipped == 1


def test_unknown_venue_is_attempted_and_fails(registry):
    """
    Someone signed up expecting to be scored. Silently omitting them is the
    worst available outcome, so it stays in the work list and reports failure.
    """
    work, skipped = post_to_supabase._plan_credential_work(
        [{"id": "p1"}], {"p1": [_credential("binance", "spot")]}
    )

    assert skipped == 0
    assert len(work) == 1
    assert post_to_supabase.sync_one_credential(work[0])["failed"] == 1


def test_participant_with_no_credentials_produces_no_work(registry):
    work, skipped = post_to_supabase._plan_credential_work([{"id": "p1"}], {})
    assert work == [] and skipped == 0


def test_credentials_of_unlisted_participants_are_ignored(registry):
    """
    get_all_api_keys() reads the whole table in one request, so it returns
    rows for participants the run isn't processing. get_active_participants()
    is what decides who is in.
    """
    registry["coinbase"] = _Venue()

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}], {"p1": [_credential()], "ghost": [_credential()]}
    )

    assert len(work) == 1


# ---------------------------------------------------------------------------
# One credential's sync
# ---------------------------------------------------------------------------

@pytest.fixture
def steps(monkeypatch):
    """Replace the three write steps; each can be told to raise."""
    calls = []

    def record(name, fail=False, value=0):
        def step(participant_id, credential, exchange, venue):
            calls.append(name)
            if fail:
                raise RuntimeError(f"{name} exploded")
            return value
        return step

    monkeypatch.setattr(post_to_supabase, "log_fetch_error", lambda *a, **k: None)
    monkeypatch.setattr(post_to_supabase, "sync_orders", record("orders", value=3))
    monkeypatch.setattr(post_to_supabase, "sync_balance_snapshot", record("snapshot"))
    monkeypatch.setattr(post_to_supabase, "sync_cash_flows", record("flows", value=2))
    return calls


def _item(**kw):
    base = {"participant_id": "p1", "credential": _credential(),
            "venue": _Venue(), "label": "coinbase/spot", "sync_flows": True}
    base.update(kw)
    return base


def test_a_clean_credential_reports_every_step(steps):
    result = post_to_supabase.sync_one_credential(_item())
    assert result == {"orders": 3, "snapshots": 1, "flows": 2,
                      "task_failures": 0, "failed": 0}
    assert steps == ["orders", "snapshot", "flows"]


def test_skipping_flows_still_syncs_everything_else(steps):
    result = post_to_supabase.sync_one_credential(_item(sync_flows=False))
    assert result["flows"] == 0
    assert result["task_failures"] == 0
    assert "flows" not in steps


def test_a_failed_order_sync_does_not_cost_the_snapshot(steps, monkeypatch):
    """
    The snapshot is the only datum that can't be backfilled. A malformed
    order must not take the equity curve point down with it.
    """
    def boom(*a, **k):
        raise RuntimeError("bad order")

    monkeypatch.setattr(post_to_supabase, "sync_orders", boom)

    result = post_to_supabase.sync_one_credential(_item())
    assert result["snapshots"] == 1
    assert result["failed"] == 0          # nothing permanent was lost
    assert result["task_failures"] == 1


def test_a_failed_snapshot_is_the_one_that_counts_as_failed(steps, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no balance")

    monkeypatch.setattr(post_to_supabase, "sync_balance_snapshot", boom)

    result = post_to_supabase.sync_one_credential(_item())
    assert result["failed"] == 1
    assert result["orders"] == 3          # the rest still ran


def test_an_unbuildable_client_fails_without_raising(steps):
    """
    A worker that raises takes down the pool and costs everyone else their
    run - the opposite of the per-credential isolation this exists for.
    """
    class Broken(_Venue):
        def build_from_credential(self, credential):
            raise ValueError("wrong FERNET_KEY")

    result = post_to_supabase.sync_one_credential(_item(venue=Broken()))
    assert result["failed"] == 1
    assert steps == []                    # nothing was attempted after


def test_no_step_raises_out_of_sync_one_credential(monkeypatch):
    """Every step failing at once still returns counters rather than raising."""
    def boom(*a, **k):
        raise RuntimeError("everything is broken")

    monkeypatch.setattr(post_to_supabase, "log_fetch_error", lambda *a, **k: None)
    for name in ("sync_orders", "sync_balance_snapshot", "sync_cash_flows"):
        monkeypatch.setattr(post_to_supabase, name, boom)

    result = post_to_supabase.sync_one_credential(_item())
    assert result == {"orders": 0, "snapshots": 0, "flows": 0,
                      "task_failures": 3, "failed": 1}


# ---------------------------------------------------------------------------
# Worker count
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("SYNC_WORKERS", raising=False)
    return monkeypatch


def test_workers_default_but_never_exceed_the_work(clean_env):
    assert post_to_supabase._worker_count(100) == post_to_supabase._DEFAULT_WORKERS
    assert post_to_supabase._worker_count(2) == 2


def test_workers_read_the_environment(clean_env):
    clean_env.setenv("SYNC_WORKERS", "3")
    assert post_to_supabase._worker_count(100) == 3


def test_one_worker_is_honoured_as_serial(clean_env):
    """The escape hatch: SYNC_WORKERS=1 must actually mean serial."""
    clean_env.setenv("SYNC_WORKERS", "1")
    assert post_to_supabase._worker_count(100) == 1


@pytest.mark.parametrize("bad", ["lots", "", "0", "-4"])
def test_unusable_worker_count_falls_back_rather_than_raising(clean_env, bad):
    """
    A malformed concurrency setting must not take down the sync, and must
    never yield 0 workers - a pool of zero would hang the run.
    """
    clean_env.setenv("SYNC_WORKERS", bad)
    assert post_to_supabase._worker_count(100) >= 1


# ---------------------------------------------------------------------------
# The resume point for account-wide transfers
#
# Only ONE credential per (participant, venue) collects an account-wide
# history, and which one depends on the order credentials come back in. The
# resume point has to survive that changing, or the whole history is written
# again under a different account_type and every deposit counts twice.
# ---------------------------------------------------------------------------

class _RecordingTable:
    """Captures the filters a query was built with."""

    def __init__(self, rows=()):
        self.filters = {}
        self._rows = list(rows)

    def select(self, *a, **k):
        return self

    def eq(self, column, value):
        self.filters[column] = value
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})


@pytest.fixture
def flow_table(monkeypatch):
    table = _RecordingTable([{"timestamp": 1_700_000_000_000}])
    monkeypatch.setattr(post_to_supabase.supabase, "table", lambda name: table)
    return table


def test_account_wide_resume_point_ignores_account_type(flow_table):
    """
    The regression this guards. A run that stored a participant's Coinbase
    deposits under 'spot' and a later run that collected them under 'perp'
    would otherwise find no history, resume from the start of the
    competition, and write every transfer a second time - and the unique key
    includes account_type, so nothing would reject it.
    """
    post_to_supabase.get_last_flow_timestamp("p1", "coinbase", None)

    assert flow_table.filters == {"participant_id": "p1", "exchange": "coinbase"}
    assert "account_type" not in flow_table.filters


def test_per_account_resume_point_still_scopes_to_account_type(flow_table):
    """
    The other direction. Lighter's histories really are separate per account
    index, so dropping the filter there would resume one credential from
    another's transfers and skip everything in between.
    """
    post_to_supabase.get_last_flow_timestamp("p1", "lighter", "perp")

    assert flow_table.filters["account_type"] == "perp"


def test_resume_point_advances_past_the_stored_transfer(flow_table):
    """+1ms, or the boundary transfer is refetched on every run."""
    assert post_to_supabase.get_last_flow_timestamp("p1", "coinbase") == \
        1_700_000_000_001


def test_no_stored_transfers_means_no_resume_point(monkeypatch):
    """None lets the adapter fall back to its own start-of-competition date."""
    monkeypatch.setattr(post_to_supabase.supabase, "table",
                        lambda name: _RecordingTable([]))
    assert post_to_supabase.get_last_flow_timestamp("p1", "coinbase") is None


def test_account_wide_venue_gets_an_unscoped_resume_lookup(monkeypatch):
    """
    End to end through sync_cash_flows: the adapter's declaration is what
    decides which lookup happens.
    """
    seen = {}

    def spy(participant_id, exchange, account_type=None):
        seen["account_type"] = account_type
        return None

    monkeypatch.setattr(post_to_supabase, "get_last_flow_timestamp", spy)

    class _Adapter:
        CASH_FLOWS_ARE_ACCOUNT_WIDE = True

        def get_cash_flows(self, exchange, since=None, portfolio_uuid=None):
            return []

    post_to_supabase.sync_cash_flows("p1", _credential(), object(), _Adapter())
    assert seen["account_type"] is None


def test_per_account_venue_gets_a_scoped_resume_lookup(monkeypatch):
    seen = {}

    def spy(participant_id, exchange, account_type=None):
        seen["account_type"] = account_type
        return None

    monkeypatch.setattr(post_to_supabase, "get_last_flow_timestamp", spy)

    class _Adapter:
        CASH_FLOWS_ARE_ACCOUNT_WIDE = False

        def get_cash_flows(self, exchange, since=None, portfolio_uuid=None):
            return []

    post_to_supabase.sync_cash_flows(
        "p1", _credential("lighter", "perp"), object(), _Adapter())
    assert seen["account_type"] == "perp"


# ---------------------------------------------------------------------------
# WHICH credential collects an account-wide history
#
# For some venues only one of a participant's credentials can read it at all.
# Picking the wrong one stops collection with no error and a green run.
# ---------------------------------------------------------------------------

class _PickyVenue(_Venue):
    """Account-wide, but only one account_type can actually read the history."""

    def __init__(self, readable_by="spot", name="coinbase"):
        super().__init__(account_wide=True, name=name)
        self.CASH_FLOWS_ACCOUNT_TYPE = readable_by


def test_the_credential_that_can_read_the_history_is_chosen(registry):
    """
    The regression this exists for. Coinbase's transfer history lives behind
    the v2 transactions endpoint, reached with the ordinary Advanced Trade
    key; an INTX perp key sees no v2 accounts and returns an empty result
    indistinguishable from "never deposited". Ordering credentials
    alphabetically put 'perp' first and handed the job to the one credential
    guaranteed to fail at it - no exception, no failed step, and every
    participant's return silently no longer adjusted for funding.
    """
    registry["coinbase"] = _PickyVenue(readable_by="spot")

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}],
        # deliberately perp-first, the order that broke it
        {"p1": [_credential(account_type="perp"),
                _credential(account_type="spot")]},
    )

    chosen = [w for w in work if w["sync_flows"]]
    assert len(chosen) == 1
    assert chosen[0]["credential"]["account_type"] == "spot"


def test_the_preferred_credential_wins_from_either_direction(registry):
    """Order of the rows must not decide it."""
    registry["coinbase"] = _PickyVenue(readable_by="spot")

    for order in (["spot", "perp"], ["perp", "spot"]):
        work, _ = post_to_supabase._plan_credential_work(
            [{"id": "p1"}],
            {"p1": [_credential(account_type=t) for t in order]},
        )
        chosen = [w for w in work if w["sync_flows"]]
        assert [c["credential"]["account_type"] for c in chosen] == ["spot"]


def test_falls_back_when_the_preferred_credential_is_not_registered(registry):
    """
    A venue that might answer beats one guaranteed not to be asked. A
    participant who registered only a perp key should still have it tried.
    """
    registry["coinbase"] = _PickyVenue(readable_by="spot")

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}], {"p1": [_credential(account_type="perp")]}
    )

    assert [w["sync_flows"] for w in work] == [True]


def test_a_venue_with_no_preference_still_collects_exactly_once(registry):
    registry["coinbase"] = _Venue(account_wide=True)

    work, _ = post_to_supabase._plan_credential_work(
        [{"id": "p1"}],
        {"p1": [_credential(account_type="spot"),
                _credential(account_type="perp")]},
    )

    assert sum(w["sync_flows"] for w in work) == 1


def test_keyless_rows_are_never_chosen_as_collector(registry):
    """
    A row with no key is skipped entirely, so choosing it as the collector
    would mean nobody collects.
    """
    registry["coinbase"] = _PickyVenue(readable_by="spot")

    work, skipped = post_to_supabase._plan_credential_work(
        [{"id": "p1"}],
        {"p1": [_credential(account_type="spot", key=None),
                _credential(account_type="perp")]},
    )

    assert skipped == 1
    assert [w["sync_flows"] for w in work] == [True]


def test_coinbase_names_the_account_type_that_can_read_its_transfers():
    """
    The declaration the planner depends on. Dropping it sends Coinbase flow
    collection back to whichever credential happens to sort first.
    """
    import coinbase
    assert coinbase.CASH_FLOWS_ACCOUNT_TYPE == "spot"
