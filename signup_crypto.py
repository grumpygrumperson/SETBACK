"""
Anonymous public-key encryption for the signup form.

The problem this solves: a signup form that writes to Supabase puts PLAINTEXT
exchange credentials in a Postgres table - and in every backup of it - until
an operator runs the importer. That defeats the entire Fernet layer, which
exists so that a database disclosure is not a credential disclosure.

So the browser encrypts before it submits. Only this service can decrypt, and
pending_signups holds ciphertext that is useless to whoever reads the table.

    browser            ephemeral keypair, ECDH against SIGNUP_PUBLIC_KEY,
                       HKDF-SHA256, AES-256-GCM       (WebCrypto, built in)
    pending_signups    {"v":1,"epk":...,"iv":...,"ct":...}
    sign_ups.py        decrypt with SIGNUP_PRIVATE_KEY, verify the credential
                       is read-only, re-encrypt with Fernet, discard

WHY NOT LIBSODIUM: a sealed box is the canonical answer and was the original
plan. It needs libsodium-wrappers in the page - around 200KB of JS and WASM
that has to be vendored, because a CDN reference is a third party able to
change the code that handles credentials. ECDH-P256 / HKDF / AES-GCM is the
same construction (ephemeral key, ECDH, authenticated cipher) built from
primitives the browser already ships and that `cryptography` already provides
for Fernet. No vendored blob, and no new dependency on either side.

The curve is P-256 rather than X25519 only for reach: WebCrypto has supported
P-256 everywhere for a decade, while X25519 support is recent and uneven.

KEY HANDLING
    SIGNUP_PRIVATE_KEY   base64, 32 bytes. Set on the machine that runs the
                         importer. NEVER on the Railway sync service - that
                         service never touches pending_signups, and a key it
                         does not hold is a key it cannot leak.
    the public key       embedded in the form, and public by design.

Losing the private key is recoverable: generate a new pair, update the form,
and ask anyone still pending to resubmit. Nothing already imported is
affected - those credentials are Fernet-encrypted under FERNET_KEY and this
key is never involved again.

Generate a pair with:

    python signup_crypto.py --generate
"""

import base64
import json
import os
import sys

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from dotenv import load_dotenv

load_dotenv()

CURVE = ec.SECP256R1()

# Mixed into HKDF so a key derived here cannot collide with one derived by
# another protocol that happens to use the same curve and KDF. Changing it
# invalidates every ciphertext in flight, so it carries the version.
CONTEXT = b"SETAPI-signup-v1"

ENVELOPE_VERSION = 1

# An envelope is ~180 bytes of overhead plus the payload. A credential set is
# a few hundred bytes; anything approaching this limit is not a signup.
MAX_ENVELOPE_BYTES = 8000


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _b64d(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def public_bytes(private_key) -> bytes:
    """The public key as an uncompressed X9.62 point - WebCrypto's 'raw'."""
    return private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def generate_keypair() -> tuple[str, str]:
    """
    A fresh (private, public) pair, both base64.

    The private half is the raw 32-byte scalar rather than PKCS8 DER, which
    makes it 44 characters - deliberately the same shape as FERNET_KEY, since
    the two sit next to each other in a .env and swapping them should fail
    loudly rather than subtly. load_private_key checks the length and says so.
    """
    private = ec.generate_private_key(CURVE)
    scalar = private.private_numbers().private_value.to_bytes(32, "big")
    return _b64e(scalar), _b64e(public_bytes(private))


def load_private_key(value: str = None):
    """
    The signup private key, from SIGNUP_PRIVATE_KEY unless one is passed.

    Raises with a usable message rather than a stack trace from inside
    cryptography: on the machine that runs the importer, a missing or
    mistyped key is by far the most likely failure.
    """
    value = value or os.getenv("SIGNUP_PRIVATE_KEY")
    if not value:
        raise RuntimeError(
            "SIGNUP_PRIVATE_KEY is not set - cannot decrypt pending signups. "
            "Generate a pair with `python signup_crypto.py --generate`, put "
            "the private half here and the public half in the signup form."
        )

    value = value.strip()

    try:
        scalar = _b64d(value)
    except Exception as e:
        # A FERNET_KEY is URL-SAFE base64 of 32 bytes: 44 characters, exactly
        # the same shape as this key, and it may contain - or _ which are not
        # in the standard alphabet. Sitting adjacent in a .env, the two get
        # swapped, so say so instead of "invalid base64".
        hint = ""
        if len(value) == 44 and ("-" in value or "_" in value):
            hint = (" It is url-safe base64 of 32 bytes, which is the shape "
                    "of a FERNET_KEY - check the two have not been swapped.")
        raise RuntimeError(
            "SIGNUP_PRIVATE_KEY is not valid base64." + hint) from e

    if len(scalar) != 32:
        raise RuntimeError(
            f"SIGNUP_PRIVATE_KEY decodes to {len(scalar)} bytes, expected 32."
        )

    return ec.derive_private_key(int.from_bytes(scalar, "big"), CURVE)


def public_key_b64(private_value: str = None) -> str:
    """The public key to paste into the signup form."""
    return _b64e(public_bytes(load_private_key(private_value)))


def _derive_key(shared_secret: bytes, server_public: bytes,
                ephemeral_public: bytes) -> bytes:
    """
    Turn the ECDH output into an AES key.

    Both public keys go into `info`, binding the derived key to the intended
    recipient and to this exact ephemeral key rather than to the raw shared
    secret alone.

    No salt: the ephemeral key already makes every derivation unique, which
    is what a salt would otherwise be providing.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=CONTEXT + server_public + ephemeral_public,
    ).derive(shared_secret)


def encrypt(payload: dict, server_public_b64: str) -> str:
    """
    Encrypt a payload to the signup public key, returning the envelope JSON.

    The browser does this in JavaScript. This exists so the round trip can be
    tested without one, and so an operator can produce a test submission by
    hand. The two implementations are checked against each other in
    tests/test_signup_crypto.py, which runs the real page's JavaScript under
    Node and decrypts the result here.
    """
    server_public = _b64d(server_public_b64)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, server_public)

    ephemeral = ec.generate_private_key(CURVE)
    ephemeral_public = public_bytes(ephemeral)

    key = _derive_key(ephemeral.exchange(ec.ECDH(), peer),
                      server_public, ephemeral_public)

    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode(), None)

    return json.dumps({
        "v": ENVELOPE_VERSION,
        "epk": _b64e(ephemeral_public),
        "iv": _b64e(iv),
        "ct": _b64e(ciphertext),
    }, separators=(",", ":"))


def decrypt(envelope: str, private_value: str = None) -> dict:
    """
    Recover a submitted payload.

    Every failure raises ValueError with a reason. The caller records it
    against the pending row rather than aborting: one submission encrypted to
    a retired public key must not stop the others being processed.
    """
    if len(envelope) > MAX_ENVELOPE_BYTES:
        raise ValueError(
            f"envelope is {len(envelope)} bytes, over the "
            f"{MAX_ENVELOPE_BYTES} limit - not a credential submission"
        )

    try:
        parsed = json.loads(envelope)
    except json.JSONDecodeError as e:
        raise ValueError(f"envelope is not valid JSON: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError("envelope is not a JSON object")

    version = parsed.get("v")
    if version != ENVELOPE_VERSION:
        raise ValueError(
            f"envelope version {version!r} is not supported (expected "
            f"{ENVELOPE_VERSION}) - the form and this service disagree"
        )

    missing = [f for f in ("epk", "iv", "ct") if not parsed.get(f)]
    if missing:
        raise ValueError(f"envelope is missing {', '.join(missing)}")

    try:
        ephemeral_public = _b64d(parsed["epk"])
        iv = _b64d(parsed["iv"])
        ciphertext = _b64d(parsed["ct"])
    except Exception as e:
        raise ValueError(f"envelope field is not valid base64: {e}") from e

    private_key = load_private_key(private_value)
    server_public = public_bytes(private_key)

    try:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(
            CURVE, ephemeral_public)
    except ValueError as e:
        # Not a point on the curve. from_encoded_point validates that, which
        # is what stops an invalid-curve attack against the ECDH below.
        raise ValueError(
            f"ephemeral public key is not a valid P-256 point: {e}"
        ) from e

    key = _derive_key(private_key.exchange(ec.ECDH(), peer),
                      server_public, ephemeral_public)

    try:
        plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    except Exception as e:
        # GCM authentication failed: wrong key, or altered ciphertext.
        #
        # The derived public key goes in the message because it is the only
        # thing that makes this diagnosable, and it is public by definition.
        # Compare it against SIGNUP_PUBLIC_KEY in signup_form.html - if they
        # differ, the form is encrypting to a key this service does not hold.
        #
        # That check also catches the one swap the length test cannot. A
        # FERNET_KEY is 32 random bytes, so roughly a quarter of them happen
        # to be valid STANDARD base64 too, load cleanly as a P-256 scalar,
        # and fail only here. Without the key printed, that failure is
        # indistinguishable from a genuinely stale envelope.
        raise ValueError(
            f"could not decrypt - the submission was encrypted to a different "
            f"public key, or has been tampered with. This service holds the "
            f"private key for {_b64e(server_public)}; check that matches "
            f"SIGNUP_PUBLIC_KEY in the form."
        ) from e

    try:
        payload = json.loads(plaintext)
    except json.JSONDecodeError as e:
        raise ValueError(f"decrypted content is not valid JSON: {e}") from e

    if not isinstance(payload, dict):
        raise ValueError("decrypted content is not a JSON object")

    return payload


if __name__ == "__main__":
    if "--generate" in sys.argv:
        private, public = generate_keypair()
        print("SIGNUP_PRIVATE_KEY (secret - put this in .env on the importer "
              "machine, NEVER on Railway):")
        print(f"  {private}")
        print()
        print("Public key (paste into signup_form.html as SIGNUP_PUBLIC_KEY):")
        print(f"  {public}")
        print()
        print("Anyone with a pending signup encrypted to a previous key will "
              "have to resubmit.")

    elif "--public" in sys.argv:
        # Prints no secret: reads the configured private key and emits only
        # the half that belongs in a web page.
        print(public_key_b64())

    else:
        print("Anonymous public-key encryption for the signup form.")
        print()
        print("  python signup_crypto.py --generate   a new keypair")
        print("  python signup_crypto.py --public     the public key, from "
              "SIGNUP_PRIVATE_KEY")
        sys.exit(2)
