"""
Tests for the run-outcome policy in post_to_supabase.

Only the pure parts: exit_code_for and _failure_threshold. The sync loop
itself talks to Supabase and Coinbase and isn't exercised here.
"""


import pytest

post_to_supabase = pytest.importorskip("post_to_supabase")

exit_code_for = post_to_supabase.exit_code_for


# ---------------------------------------------------------------------------
# exit_code_for
# ---------------------------------------------------------------------------

def test_clean_run_succeeds():
    assert exit_code_for(attempted=200, failed=0) == 0


def test_one_failure_out_of_many_still_succeeds():
    """
    The case this whole function exists for. One participant revoking their
    key must not make every subsequent run red - a permanently red job is one
    nobody looks at.
    """
    assert exit_code_for(attempted=200, failed=1) == 0


def test_total_failure_fails():
    """Wrong FERNET_KEY, Supabase down - nothing will fix itself."""
    assert exit_code_for(attempted=200, failed=200) == 1


def test_nothing_attempted_fails():
    """
    An empty participants table means registration is broken. Reporting
    success for having done nothing is how that goes unnoticed - it already
    happened once.
    """
    assert exit_code_for(attempted=0, failed=0) == 1


def test_zero_failures_succeeds_at_every_threshold():
    """A run with no failures is never an error, whatever the policy."""
    for threshold in (0.0, 0.25, 0.5, 1.0):
        assert exit_code_for(attempted=10, failed=0, threshold=threshold) == 0


def test_threshold_zero_fails_on_any_failure():
    assert exit_code_for(attempted=200, failed=1, threshold=0.0) == 1


@pytest.mark.parametrize("failed,expected", [
    (49, 0),    # under half
    (50, 1),    # exactly half - threshold is inclusive
    (51, 1),    # over half
])
def test_half_threshold_boundary(failed, expected):
    assert exit_code_for(attempted=100, failed=failed, threshold=0.5) == expected


def test_single_credential_failing_is_a_total_failure():
    """With one credential there is no partial - it worked or it didn't."""
    assert exit_code_for(attempted=1, failed=1) == 1
    assert exit_code_for(attempted=1, failed=0) == 0


def test_failed_never_exceeds_attempted_in_practice():
    """
    Guards the accounting change: `failed` counts CREDENTIALS, not steps. It
    used to count every failed step, and one credential can fail three of
    them - which would push the ratio above 1.0 and make the threshold
    meaningless.
    """
    assert exit_code_for(attempted=4, failed=4, threshold=1.0) == 1
    assert exit_code_for(attempted=4, failed=3, threshold=1.0) == 0


# ---------------------------------------------------------------------------
# _failure_threshold
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("SYNC_FAILURE_THRESHOLD", raising=False)
    return monkeypatch


def test_threshold_defaults_to_total_failure(clean_env):
    assert post_to_supabase._failure_threshold() == 1.0


def test_threshold_reads_the_environment(clean_env):
    clean_env.setenv("SYNC_FAILURE_THRESHOLD", "0.5")
    assert post_to_supabase._failure_threshold() == 0.5


@pytest.mark.parametrize("bad", ["half", "", "1.5", "-0.1", "abc"])
def test_unusable_threshold_falls_back_rather_than_raising(clean_env, bad):
    """
    A malformed alerting policy must not take down the sync. Getting the data
    in matters more than the exit code being exactly right.
    """
    clean_env.setenv("SYNC_FAILURE_THRESHOLD", bad)
    assert post_to_supabase._failure_threshold() == 1.0
