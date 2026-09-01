"""
The signup form agrees with the table it writes to and the code that reads it.

Three artifacts have to use the same field names - an HTML page, a SQL table,
and a Python importer - and nothing about that is checked by any of the three
languages involved. The failure is quiet in the worst way: a misspelled field
is simply absent from the POST body, so the row inserts fine with a NULL
credential, and the participant is later told their API key was invalid.
Nobody looks for a typo in a form when the exchange appears to be rejecting a
key.

There is no browser cryptography here any more. The form POSTs plaintext to
pending_signups and sign_ups.py encrypts it under FERNET_KEY on the way into
participant_api_keys - which means the DRAINING is what bounds exposure, and
what these tests can usefully protect is that the plumbing lines up.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FORM = REPO_ROOT / "signup_form.html"
SCHEMA = REPO_ROOT / "migrations" / "schema.sql"

PORTABLE = re.compile(
    r"// ===== BEGIN PORTABLE LOGIC.*?\n(.*?)// ===== END PORTABLE LOGIC", re.S)


def _portable_source() -> str:
    match = PORTABLE.search(FORM.read_text(encoding="utf-8"))
    assert match, ("the PORTABLE LOGIC markers are gone from signup_form.html "
                   "- these tests cannot find the code they check")
    return match.group(1)


def _submit_fields() -> list[str]:
    """The field names the form POSTs, read out of the page itself."""
    block = re.search(r"const SUBMIT_FIELDS = \[(.*?)\];", _portable_source(), re.S)
    assert block, "SUBMIT_FIELDS is missing from signup_form.html"
    return re.findall(r'"([a-z_]+)"', block.group(1))


def _pending_signups_columns() -> set[str]:
    """Columns declared on pending_signups in schema.sql."""
    sql = SCHEMA.read_text(encoding="utf-8")
    body = re.search(
        r"create table if not exists public\.pending_signups \((.*?)\n\);",
        sql, re.S)
    assert body, "pending_signups is not declared in schema.sql"

    columns = set()
    for line in body.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        name = line.split()[0]
        if name.isidentifier():
            columns.add(name)
    return columns


# ---------------------------------------------------------------------------
# The three artifacts agree
# ---------------------------------------------------------------------------

def test_every_submitted_field_is_a_real_column():
    """A field the form sends that the table does not have is a 400 at submit."""
    missing = set(_submit_fields()) - _pending_signups_columns()
    assert not missing, (
        f"signup_form.html POSTs {sorted(missing)}, which pending_signups "
        f"does not have. Supabase will reject the insert."
    )


def test_every_field_the_importer_reads_is_one_the_form_sends():
    """
    The direction that fails SILENTLY.

    An extra column in the table is harmless. A column the importer reads but
    the form never sends arrives as None, and the participant is told their
    credential was invalid.
    """
    sign_ups = pytest.importorskip("sign_ups")
    source = Path(sign_ups.__file__).read_text(encoding="utf-8")

    select = re.search(r'\.select\("([^"]+(?:"\s*\n\s*"[^"]+)*)"\)', source)
    assert select, "could not find the pending_signups select in sign_ups.py"

    requested = {c.strip() for c in
                 re.sub(r'"\s*\n\s*"', "", select.group(1)).split(",")}

    # Columns the importer manages itself rather than receiving from the form.
    bookkeeping = {"id", "attempts", "status", "last_error",
                   "submitted_at", "processed_at"}

    missing = requested - bookkeeping - set(_submit_fields())
    assert not missing, (
        f"sign_ups.py reads {sorted(missing)} from pending_signups but the "
        f"form never sends it - it would always arrive as None"
    )


def test_the_form_is_committed_unconfigured():
    """
    The committed copy carries placeholders, not a live endpoint.

    Neither value is secret - the anon key is public by design and RLS is what
    constrains it - but a real project URL committed here is a live endpoint
    in a public repository, and a fork submitting real credentials to it is
    nobody's intention.
    """
    text = FORM.read_text(encoding="utf-8")
    for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
        assert f'{name}: "REPLACE_WITH_{name}"' in text, \
            f"{name} in signup_form.html is not a placeholder"


def test_a_resolved_row_cannot_keep_a_credential():
    """
    The constraint that bounds how long plaintext lives in the table.

    Asserted on the schema text because it is the property the whole
    simplified design rests on: the importer wipes the credential, and the
    database refuses to record the row as resolved if it did not.
    """
    sql = SCHEMA.read_text(encoding="utf-8")
    assert "pending_signups_resolved_is_empty" in sql
    for column in ("api_key", "api_secret", "api_passphrase"):
        assert f"{column} is null" in sql, \
            f"the resolved-is-empty constraint does not cover {column}"


def test_the_importer_wipes_every_credential_column():
    """The other half of the same property, in the code that does the wiping."""
    sign_ups = pytest.importorskip("sign_ups")
    source = Path(sign_ups.__file__).read_text(encoding="utf-8")

    resolve = source[source.index("def _resolve_signup"):
                     source.index("def _defer_signup")]

    for column in ("api_key", "api_secret", "api_passphrase"):
        assert f'"{column}": None' in resolve, \
            f"_resolve_signup does not clear {column}"


# ---------------------------------------------------------------------------
# The form's own guard, exercised rather than read
# ---------------------------------------------------------------------------

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_form_refuses_a_lighter_wallet_key(tmp_path):
    """
    Only a courtesy - lighter.verify_credential is the real control - but it
    is the one check worth having client-side, because it stops an
    irrevocable wallet private key being transmitted at all rather than
    rejecting it after receipt.
    """
    script = tmp_path / "guard.js"
    script.write_text(
        _portable_source() +
        "\nconsole.log(JSON.stringify("
        "localCredentialProblem(JSON.parse(process.argv[2]))));\n",
        encoding="utf-8")

    def check(fields):
        out = subprocess.run([NODE, str(script), json.dumps(fields)],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip())

    wallet_key = {"exchange": "lighter", "account_type": "perp",
                  "api_key": "0x" + "a" * 64, "api_secret": ""}
    assert "read-only token" in (check(wallet_key) or "")

    token = {"exchange": "lighter", "account_type": "perp",
             "api_key": "ro:741152:all:1790000000:" + "d" * 64,
             "api_secret": ""}
    assert check(token) is None

    spot_lighter = {"exchange": "lighter", "account_type": "spot",
                    "api_key": "ro:741152:all:1790000000:d", "api_secret": ""}
    assert "no spot market" in (check(spot_lighter) or "")

    no_secret = {"exchange": "coinbase", "account_type": "spot",
                 "api_key": "k", "api_secret": ""}
    assert "API secret" in (check(no_secret) or "")

    good_coinbase = {"exchange": "coinbase", "account_type": "spot",
                     "api_key": "k", "api_secret": "s"}
    assert check(good_coinbase) is None
