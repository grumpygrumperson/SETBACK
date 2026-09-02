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


def _credential_columns() -> list[str]:
    """
    The submitted fields that carry a secret.

    Derived from SUBMIT_FIELDS rather than listed here, so the two tests
    below automatically cover any credential field added to the form later.
    That matters: the failure they guard against is a new secret column
    nobody remembered to wipe.
    """
    return [f for f in _submit_fields() if f.startswith("api_")]


def test_a_resolved_row_cannot_keep_a_credential():
    """
    The constraint that bounds how long plaintext lives in the table.

    Asserted on the schema text because this is the property the whole
    simplified design rests on: the importer wipes the credential, and the
    database refuses to record the row as resolved if it did not.
    """
    sql = SCHEMA.read_text(encoding="utf-8")

    # The ADD, not the `drop constraint if exists` that precedes it - which is
    # what this first matched, and which trivially contains the name and
    # nothing else.
    start = sql.index("add constraint pending_signups_resolved_is_empty")
    constraint = sql[start:sql.index(";", start)]

    for column in _credential_columns():
        assert f"{column} is null" in constraint, \
            f"the resolved-is-empty constraint does not cover {column}"


def test_the_importer_wipes_every_credential_column():
    """The other half of the same property, in the code that does the wiping."""
    sign_ups = pytest.importorskip("sign_ups")
    source = Path(sign_ups.__file__).read_text(encoding="utf-8")

    resolve = source[source.index("def _resolve_signup"):
                     source.index("def _defer_signup")]

    for column in _credential_columns():
        assert f'"{column}": None' in resolve, \
            f"_resolve_signup does not clear {column}"


# ---------------------------------------------------------------------------
# The form's own guard, exercised rather than read
# ---------------------------------------------------------------------------

NODE = shutil.which("node")


@pytest.fixture(scope="module")
def build_rows(tmp_path_factory):
    """
    buildRows() from the page, callable from Python.

    Runs the real JavaScript under Node rather than reimplementing it here.
    A reimplementation would test the copy, which is the one thing that
    cannot be wrong in production.
    """
    if NODE is None:
        pytest.skip("node is not installed")

    script = tmp_path_factory.mktemp("form") / "rows.js"
    script.write_text(
        _portable_source() +
        "\nconsole.log(JSON.stringify(buildRows(JSON.parse(process.argv[2]))));\n",
        encoding="utf-8")

    def call(values):
        out = subprocess.run([NODE, str(script), json.dumps(values)],
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip())

    return call


IDENTITY = {"display_name": "Ada", "email": "ada@example.com"}
TOKEN = "ro:741152:all:1790000000:" + "d" * 64


def test_all_three_venues_register_from_one_submission(build_rows):
    """The point of the form's shape: one submission, every venue."""
    result = build_rows({**IDENTITY,
                         "coinbase_spot_key": "k1", "coinbase_spot_secret": "s1",
                         "coinbase_perp_key": "k2", "coinbase_perp_secret": "s2",
                         "lighter_token": TOKEN})

    assert result["problem"] is None
    assert [(r["exchange"], r["account_type"]) for r in result["rows"]] == [
        ("coinbase", "spot"), ("coinbase", "perp"), ("lighter", "perp")]

    # Identity is repeated on every row; register_participant upserts on email,
    # so the three rows resolve to one participant with three venues.
    assert {r["email"] for r in result["rows"]} == {"ada@example.com"}


def test_blank_sections_are_skipped(build_rows):
    """Someone who trades one venue submits one row, not three empty ones."""
    result = build_rows({**IDENTITY, "lighter_token": TOKEN})

    assert result["problem"] is None
    assert len(result["rows"]) == 1
    assert result["rows"][0]["exchange"] == "lighter"
    assert result["rows"][0]["api_secret"] is None    # single-credential venue


def test_at_least_one_venue_is_required(build_rows):
    result = build_rows(IDENTITY)
    assert result["rows"] == []
    assert "at least one venue" in result["problem"]


def test_a_lighter_wallet_key_is_refused_before_it_is_sent(build_rows):
    """
    Only a courtesy - lighter.verify_credential is the real control - but it
    is the one client-side check worth having, because it stops an
    irrevocable wallet private key being transmitted at all rather than
    rejecting it after receipt.
    """
    result = build_rows({**IDENTITY, "lighter_token": "0x" + "a" * 64})

    assert result["rows"] == []
    assert "read-only token" in result["problem"]


def test_coinbase_needs_both_halves(build_rows):
    key_only = build_rows({**IDENTITY, "coinbase_spot_key": "k"})
    assert "both the API key name and the private key" in key_only["problem"]

    secret_only = build_rows({**IDENTITY, "coinbase_spot_secret": "s"})
    assert "no API key name" in secret_only["problem"]


def test_one_bad_venue_blocks_the_whole_submission(build_rows):
    """
    Deliberate: the POST is one request, so a partially valid submission
    would insert some venues and silently drop others. Better to say what is
    wrong while the participant is still looking at the form.
    """
    result = build_rows({**IDENTITY,
                         "coinbase_spot_key": "k1", "coinbase_spot_secret": "s1",
                         "lighter_token": "0x" + "a" * 64})

    assert result["rows"] == []
    assert "read-only token" in result["problem"]


def test_submitted_rows_carry_exactly_the_declared_fields(build_rows):
    """
    Rows must not grow a field SUBMIT_FIELDS does not list, since that list
    is what the column checks above are asserted against.
    """
    result = build_rows({**IDENTITY, "coinbase_spot_key": "k",
                         "coinbase_spot_secret": "s"})

    assert set(result["rows"][0]) == set(_submit_fields())


def test_venue_values_satisfy_the_schema_constraints():
    """
    The exchange and account_type each venue emits must be values
    pending_signups actually allows, or every insert fails on a check
    constraint.
    """
    source = _portable_source()
    sql = SCHEMA.read_text(encoding="utf-8")

    exchanges = set(re.findall(r'exchange: "(\w+)"', source))
    account_types = set(re.findall(r'account_type: "(\w+)"', source))

    allowed_exchanges = set(re.findall(
        r"pending_signups_exchange_check\s+check \(exchange in \(([^)]*)\)\)",
        sql)[0].replace("'", "").split(", "))
    allowed_types = set(re.findall(
        r"pending_signups_account_type_check\s+check \(account_type in \(([^)]*)\)\)",
        sql)[0].replace("'", "").split(", "))

    assert exchanges <= allowed_exchanges, \
        f"form emits {sorted(exchanges - allowed_exchanges)}, which the schema rejects"
    assert account_types <= allowed_types, \
        f"form emits {sorted(account_types - allowed_types)}, which the schema rejects"
