"""
Tests for the signup envelope, and for the two implementations of it.

The credential path now crosses a language boundary: JavaScript in the
browser encrypts, Python decrypts. Nothing about that is checked by types, by
imports, or by either language's tooling - and a mismatch does not fail
loudly. It produces submissions that look fine to the participant, land in
the database, and can never be opened by anyone.

So the interop test here extracts the REAL JavaScript out of
signup_form.html, runs it under Node, and decrypts the result with
signup_crypto. If someone edits one side and not the other, that test fails.
It is the only thing in the repository that can catch it.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

signup_crypto = pytest.importorskip("signup_crypto")

REPO_ROOT = Path(__file__).resolve().parent.parent
FORM = REPO_ROOT / "signup_form.html"

PAYLOAD = {
    "api_key": "organizations/abc/apiKeys/def",
    "api_secret": "-----BEGIN EC PRIVATE KEY-----\nnot-a-real-key\n-----END EC PRIVATE KEY-----",
    "api_passphrase": None,
    "exchange": "coinbase",
    "account_type": "spot",
}


@pytest.fixture(scope="module")
def keypair():
    return signup_crypto.generate_keypair()


# ---------------------------------------------------------------------------
# The envelope, in Python
# ---------------------------------------------------------------------------

def test_round_trip(keypair):
    private, public = keypair
    assert signup_crypto.decrypt(
        signup_crypto.encrypt(PAYLOAD, public), private) == PAYLOAD


def test_every_envelope_is_different(keypair):
    """
    A fresh ephemeral key per submission.

    Without it, two participants submitting the same credential would produce
    identical ciphertext, and anyone reading the table could tell - which is
    a fact about their keys leaking out of a table that is supposed to reveal
    nothing.
    """
    _, public = keypair
    first = signup_crypto.encrypt(PAYLOAD, public)
    second = signup_crypto.encrypt(PAYLOAD, public)

    assert first != second
    assert json.loads(first)["epk"] != json.loads(second)["epk"]


def test_a_different_key_cannot_decrypt(keypair):
    _, public = keypair
    other_private, _ = signup_crypto.generate_keypair()

    with pytest.raises(ValueError, match="could not decrypt"):
        signup_crypto.decrypt(
            signup_crypto.encrypt(PAYLOAD, public), other_private)


def test_tampering_is_detected(keypair):
    """
    AES-GCM authenticates. A row edited in the database - by anyone who
    reaches it - must fail to open rather than decrypt to something else.
    """
    private, public = keypair
    envelope = json.loads(signup_crypto.encrypt(PAYLOAD, public))

    raw = bytearray(signup_crypto._b64d(envelope["ct"]))
    raw[0] ^= 0x01
    envelope["ct"] = signup_crypto._b64e(bytes(raw))

    with pytest.raises(ValueError, match="could not decrypt"):
        signup_crypto.decrypt(json.dumps(envelope), private)


def test_a_swapped_ephemeral_key_is_detected(keypair):
    """
    The ephemeral key is bound into the HKDF info, so replacing it with
    another valid P-256 point cannot yield a working key.
    """
    private, public = keypair
    envelope = json.loads(signup_crypto.encrypt(PAYLOAD, public))
    other = json.loads(signup_crypto.encrypt(PAYLOAD, public))
    envelope["epk"] = other["epk"]

    with pytest.raises(ValueError, match="could not decrypt"):
        signup_crypto.decrypt(json.dumps(envelope), private)


@pytest.mark.parametrize("envelope,expected", [
    ("not json at all", "not valid JSON"),
    ('["a","list"]', "not a JSON object"),
    ('{"v":99,"epk":"a","iv":"b","ct":"c"}', "version"),
    ('{"v":1,"iv":"b","ct":"c"}', "missing epk"),
    ('{"v":1,"epk":"a","ct":"c"}', "missing iv"),
    ('{"v":1,"epk":"!!!","iv":"!!!","ct":"!!!"}', "not valid base64"),
])
def test_a_malformed_envelope_says_what_is_wrong(keypair, envelope, expected):
    """
    Every failure names a reason, because the reason is stored against the
    pending row and is the only thing an operator has to go on when telling a
    participant why their submission did not work.
    """
    private, _ = keypair
    with pytest.raises(ValueError, match=expected):
        signup_crypto.decrypt(envelope, private)


def test_an_oversized_envelope_is_refused_before_parsing(keypair):
    private, _ = keypair
    with pytest.raises(ValueError, match="over the .* limit"):
        signup_crypto.decrypt("x" * 9000, private)


def test_an_invalid_curve_point_is_rejected(keypair):
    """
    from_encoded_point validates the point is on the curve. Skipping that is
    the invalid-curve attack: a crafted point can leak bits of the private
    key through the shared secret.
    """
    private, public = keypair
    envelope = json.loads(signup_crypto.encrypt(PAYLOAD, public))
    envelope["epk"] = signup_crypto._b64e(b"\x04" + b"\x01" * 64)

    with pytest.raises(ValueError, match="not a valid P-256 point"):
        signup_crypto.decrypt(json.dumps(envelope), private)


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def test_a_generated_private_key_is_the_length_a_fernet_key_is():
    """
    Both are 44 base64 characters, on purpose: they sit adjacent in a .env
    and confusing them should be caught by the next test, not by a confusing
    failure somewhere downstream.
    """
    private, public = signup_crypto.generate_keypair()
    assert len(private) == 44
    assert len(signup_crypto._b64d(public)) == 65      # uncompressed point


def test_a_url_safe_fernet_key_is_named_as_the_likely_mistake():
    """
    The mistake this shape invites, caught where it happens.

    A FERNET_KEY is url-safe base64, so most of them contain - or _ and fail
    to parse as standard base64. Those can be identified confidently.
    """
    from cryptography.fernet import Fernet

    # Most Fernet keys contain a url-safe character; take one that does.
    key = next(k for k in (Fernet.generate_key().decode() for _ in range(200))
               if "-" in k or "_" in k)

    with pytest.raises(RuntimeError, match="check the two have not been swapped"):
        signup_crypto.load_private_key(key)


def test_a_fernet_key_that_parses_is_caught_at_decryption(keypair):
    """
    The swap the length check CANNOT catch, and why decrypt names the key.

    A FERNET_KEY is 32 random bytes. Around a quarter of them contain no
    url-safe-only character, so they are valid STANDARD base64 as well,
    decode to exactly 32 bytes, and load as a perfectly good P-256 scalar.
    Nothing about the key itself is wrong - it is simply the wrong key.

    The only place that surfaces is decryption, so the message there has to
    carry enough to diagnose it: the public key this service actually holds,
    to compare against the one in the form.
    """
    from cryptography.fernet import Fernet

    plausible = next(
        k for k in (Fernet.generate_key().decode() for _ in range(500))
        if "-" not in k and "_" not in k)

    # It loads without complaint - that is the point.
    signup_crypto.load_private_key(plausible)

    _, public = keypair
    envelope = signup_crypto.encrypt(PAYLOAD, public)

    with pytest.raises(ValueError, match="This service holds the private key for"):
        signup_crypto.decrypt(envelope, plausible)


def test_a_missing_key_says_how_to_make_one(monkeypatch):
    monkeypatch.delenv("SIGNUP_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError, match="--generate"):
        signup_crypto.load_private_key()


def test_the_public_key_is_derived_not_stored(keypair):
    private, public = keypair
    assert signup_crypto.public_key_b64(private) == public


# ---------------------------------------------------------------------------
# The browser half
#
# These read signup_form.html directly. The page is not built or bundled, so
# the file IS the deployed artifact and there is nothing else to test.
# ---------------------------------------------------------------------------

CRYPTO_BLOCK = re.compile(
    r"// ===== BEGIN PORTABLE CRYPTO.*?\n(.*?)// ===== END PORTABLE CRYPTO",
    re.S)


def _browser_crypto_source() -> str:
    match = CRYPTO_BLOCK.search(FORM.read_text(encoding="utf-8"))
    assert match, ("the PORTABLE CRYPTO markers are gone from "
                   "signup_form.html - the interop test cannot find the code "
                   "it is supposed to be checking")
    return match.group(1)


def test_the_form_exists_and_is_unconfigured():
    """
    The committed copy must carry placeholders, not real values.

    A Supabase URL or anon key committed here would be a live endpoint in the
    repository, and a real SIGNUP_PUBLIC_KEY would silently make a fork of
    this project submit credentials to whoever holds the private half.
    """
    text = FORM.read_text(encoding="utf-8")
    for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SIGNUP_PUBLIC_KEY"):
        assert f'{name}: "REPLACE_WITH_{name}"' in text, \
            f"{name} in signup_form.html is not a placeholder"


def test_the_two_implementations_agree_on_the_context_string():
    """
    CONTEXT goes into HKDF, so a difference of one byte produces different
    keys and every submission becomes permanently unopenable. Cheap to check
    without Node, so it is checked separately.
    """
    source = _browser_crypto_source()
    expected = signup_crypto.CONTEXT.decode()

    assert f'const CONTEXT = "{expected}"' in source
    assert f"const ENVELOPE_VERSION = {signup_crypto.ENVELOPE_VERSION};" in source


# ---------------------------------------------------------------------------
# The interop test itself
# ---------------------------------------------------------------------------

NODE = shutil.which("node")

_HARNESS = """
const payload = JSON.parse(process.argv[2]);
sealToPublicKey(payload, process.argv[3])
  .then(envelope => process.stdout.write(envelope))
  .catch(error => { console.error(error); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_javascript_encrypts_what_python_can_decrypt(keypair, tmp_path):
    """
    The test this module exists for.

    Runs the page's own JavaScript under Node's WebCrypto - the same
    implementation the browser uses - and opens the result with the real
    decryptor. It covers every detail the two sides have to agree on, and
    that nothing else checks: the curve, the KDF, the info string, the IV
    length, the base64 alphabet, and the envelope's field names.

    In particular it settles the empty-salt question. cryptography takes
    salt=None and WebCrypto requires the parameter, so the page passes an
    empty array. RFC 5869 says they are equivalent - an absent salt means
    HashLen zeros, and HMAC zero-pads a short key - but "should be
    equivalent" is not something to find out is wrong once participants have
    submitted.
    """
    private, public = keypair

    script = tmp_path / "crypto.js"
    script.write_text(_browser_crypto_source() + _HARNESS, encoding="utf-8")

    result = subprocess.run(
        [NODE, str(script), json.dumps(PAYLOAD), public],
        capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, f"node failed: {result.stderr}"

    assert signup_crypto.decrypt(result.stdout.strip(), private) == PAYLOAD


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_javascript_rejects_a_lighter_wallet_key(tmp_path):
    """
    The form's own guard, exercised rather than read.

    It is only a courtesy - anyone can POST straight to the endpoint, and
    lighter.verify_credential is the actual control - but a participant who
    pastes a wallet private key should be told immediately, not by email
    after it has been transmitted.
    """
    script = tmp_path / "guard.js"
    script.write_text(
        _browser_crypto_source() +
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

    no_secret = {"exchange": "coinbase", "account_type": "spot",
                 "api_key": "k", "api_secret": ""}
    assert "API secret" in (check(no_secret) or "")
