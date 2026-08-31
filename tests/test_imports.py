"""
Every module must import, and the list below must be complete.

This exists because a green test suite was not evidence that the code ran.
Two failures reached `main` that the other 264 tests could not have caught:

  1. metrics.py referenced venue_common.COMPETITION_END before venue_common
     defined it. The name is read inside a function, so the module IMPORTED
     fine - it raised AttributeError only when scoring ran, which is wrapped
     in try/except, so the symptom was a stale leaderboard rather than a
     crash. Commit e6d2808 fixed it.

  2. lighter.py was untracked - present locally, absent from the repository.
     Tests reach their modules through pytest.importorskip, which SKIPS when
     a module is missing rather than failing. Deleting metrics.py from a
     checkout turns `264 passed` into `186 passed, 2 skipped` and still exits
     0: 78 tests vanish and the suite reports success.

The second is what makes the explicit list below necessary rather than
discovering modules from disk. A module that is missing cannot be discovered,
so discovery would silently shrink to whatever survived. The list is checked
against the tracked file set in the other direction, so a NEW module cannot
escape the guard either.

Running this in CI against a fresh checkout is what makes it meaningful: CI
sees only what was committed, which is exactly the tree that broke twice.
"""

import importlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every top-level module the service needs. Kept explicit on purpose - see
# the module docstring.
REQUIRED_MODULES = (
    "coinbase",
    "lighter",
    "metrics",
    "post_to_supabase",
    "rotate_credentials",
    "score",
    "sign_ups",
    "venue_common",
    "venues",
)


@pytest.mark.parametrize("name", REQUIRED_MODULES)
def test_module_imports(name):
    """
    Import for real - no importorskip, which is the whole point.

    conftest.py installs placeholder SUPABASE_URL / SUPABASE_KEY / FERNET_KEY
    before this runs, so the modules that build a Supabase client at import
    time work with no .env present. Nothing here connects: create_client()
    only constructs.
    """
    importlib.import_module(name)


def _tracked_top_level_modules() -> set[str] | None:
    """
    Module names for the tracked top-level .py files, or None if unavailable.

    Deliberately asks git rather than globbing the directory: several scratch
    files (main.py, test.py, API_endpoints.py) sit in the working tree and are
    gitignored precisely because they are not part of the service. Globbing
    would demand they import, and they hold credentials and side effects.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if out.returncode != 0:
        return None

    return {
        Path(line).stem
        for line in out.stdout.splitlines()
        if line.strip() and "/" not in line.strip()
    }


def test_required_modules_covers_every_tracked_module():
    """
    A new module must be added to REQUIRED_MODULES, not merely committed.

    Without this, the guard above protects only what someone remembered to
    list, and the next module added is unprotected from the day it lands.
    """
    tracked = _tracked_top_level_modules()
    if tracked is None:
        pytest.skip("git unavailable - cannot determine the tracked file set")

    missing = tracked - set(REQUIRED_MODULES)
    assert not missing, (
        f"tracked module(s) not in REQUIRED_MODULES: {sorted(missing)}. "
        f"Add them so they are import-checked."
    )


def test_required_modules_are_all_tracked():
    """
    The mirror: a listed module that is not committed is the lighter.py bug.

    It passes locally - the file is right there - and fails in CI, which is
    the correct place to find out, and the reason CI runs this at all.
    """
    tracked = _tracked_top_level_modules()
    if tracked is None:
        pytest.skip("git unavailable - cannot determine the tracked file set")

    untracked = set(REQUIRED_MODULES) - tracked
    assert not untracked, (
        f"module(s) required but not committed: {sorted(untracked)}. "
        f"They exist locally and would be absent from a fresh checkout."
    )
