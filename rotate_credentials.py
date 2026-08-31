"""
Re-encrypt every stored credential under the current FERNET_KEY.

Run this after rotating the key. The sequence is:

  1. Generate a new key       python gen_fernet_key.py
  2. In the environment:      FERNET_KEY          = <new key>
                              FERNET_KEYS_RETIRED = <old key>
  3. python rotate_credentials.py
  4. Once it reports everything re-encrypted, remove FERNET_KEYS_RETIRED.

Between steps 2 and 4 the system keeps working: MultiFernet decrypts with
either key, so the sync never sees an interruption. Step 4 is what actually
retires the old key - until then a leaked old key still opens everything.

Safe to re-run. Rows already on the current key are re-encrypted to an
equivalent token, so a partial run just needs running again.
"""

import os

from dotenv import load_dotenv
from supabase import create_client

from venue_common import get_fernet
from cryptography.fernet import Fernet, InvalidToken

load_dotenv()

supabase = create_client(os.getenv("SUPABASE_URL"),
                         os.getenv("SUPABASE_KEY"))

# The encrypted columns of participant_api_keys. api_passphrase is nullable,
# so it's handled per-row rather than assumed present.
ENCRYPTED_COLUMNS = ("api_key", "api_secret", "api_passphrase")


def rotate_all() -> None:
    """
    Walk every credential row and rewrite its encrypted columns under the
    current key.

    MultiFernet.rotate() decrypts with whichever key works and re-encrypts
    with the first - so this needs no record of which key a row was written
    under, and no key_version column to drift out of sync with reality.
    """
    fernet = get_fernet()

    rows = supabase.table("participant_api_keys").select(
        "id,participant_id,api_key,api_secret,api_passphrase"
    ).execute().data or []

    rotated = 0
    failed = 0

    for row in rows:
        updates = {}
        try:
            for column in ENCRYPTED_COLUMNS:
                value = row.get(column)
                if not value:
                    continue                     # nullable, e.g. no passphrase
                updates[column] = fernet.rotate(value.encode()).decode()

            if updates:
                supabase.table("participant_api_keys") \
                    .update(updates).eq("id", row["id"]).execute()
                rotated += 1

        except Exception as e:
            # Most likely a row encrypted under a key that's in neither
            # FERNET_KEY nor FERNET_KEYS_RETIRED - it cannot be recovered
            # here, and that participant has to re-register.
            failed += 1
            print(f"FAILED id={row['id']} participant={row.get('participant_id')}: {e}")

    print(f"Re-encrypted {rotated} of {len(rows)} credential row(s), {failed} failed.")
    if failed:
        print("Leave FERNET_KEYS_RETIRED in place - some rows are still on an "
              "older key and removing it would strand them permanently.")
    else:
        print("All rows are on the current key. FERNET_KEYS_RETIRED can now be "
              "removed from the environment.")


def report_credential_shapes() -> int:
    """
    Audit what is ALREADY stored, and name any credential the competition
    should not be holding.

    Signup-time validation only protects new registrations. A Lighter wallet
    private key registered before that gate existed sits in the table and
    keeps working - and it is the one credential type that cannot be made safe
    after the fact, because it cannot be revoked. Finding those is the whole
    point of this function.

    Deliberately here rather than in a new script: rotate_all() above already
    decrypts every row in a loop, so this reuses the one place in the codebase
    that legitimately holds plaintext credentials instead of creating a second.

    Prints participant_id and shape only. It must never print a value, and
    "just log the first few characters to help debug" is how that rule gets
    broken - the prefix of a private key is still key material.

    Returns the number of credentials that need attention.
    """
    fernet = get_fernet()

    rows = supabase.table("participant_api_keys").select(
        "id,participant_id,exchange,account_type,api_key,is_active"
    ).execute().data or []

    unsafe = 0
    unreadable = 0

    for row in rows:
        exchange = row.get("exchange") or "coinbase"
        label = f"id={row['id']} participant={row.get('participant_id')} " \
                f"{exchange}/{row.get('account_type')}"

        try:
            plaintext = fernet.decrypt((row.get("api_key") or "").encode()).decode()
        except Exception as e:
            unreadable += 1
            print(f"UNREADABLE {label}: {type(e).__name__}")
            continue

        if exchange == "lighter" and not plaintext.strip().startswith("ro:"):
            unsafe += 1
            print(f"WALLET KEY  {label} active={row.get('is_active')}")

    print(f"\nChecked {len(rows)} credential(s): {unsafe} wallet key(s), "
          f"{unreadable} unreadable.")

    if unsafe:
        print(
            "\nA wallet private key controls every asset in that wallet and "
            "cannot be revoked.\n"
            "  1. The sync already refuses these - those participants are not "
            "being scored.\n"
            "  2. Ask each to register a read-only token instead.\n"
            "  3. Tell them to MOVE THEIR FUNDS to a new wallet. The key was "
            "exposed to this\n"
            "     service and to whatever path it travelled here; rotating it "
            "is not possible."
        )

    return unsafe


def verify_current_key_only() -> None:
    """
    Confirm every row decrypts under FERNET_KEY ALONE.

    Run this before deleting FERNET_KEYS_RETIRED. It's the only honest check:
    with the retired key still loaded, MultiFernet would happily decrypt rows
    that haven't been rotated and the rotation would look complete when it
    isn't.
    """

    current = Fernet(os.getenv("FERNET_KEY").strip().encode())
    rows = supabase.table("participant_api_keys").select(
        "id,api_key,api_secret,api_passphrase"
    ).execute().data or []

    stale = []
    for row in rows:
        for column in ENCRYPTED_COLUMNS:
            value = row.get(column)
            if not value:
                continue
            try:
                current.decrypt(value.encode())
            except InvalidToken:
                stale.append((row["id"], column))

    if stale:
        print(f"{len(stale)} value(s) NOT on the current key: {stale[:10]}")
        print("Run rotate_all() again before retiring the old key.")
    else:
        print(f"All {len(rows)} row(s) decrypt under FERNET_KEY alone - safe to "
              f"remove FERNET_KEYS_RETIRED.")


if __name__ == "__main__":
    import sys

    # The audit is read-only and answers a different question from rotation,
    # so it must be runnable WITHOUT rewriting every credential first. Asking
    # "is anyone's wallet key in here?" should never require a key rotation.
    if "--audit" in sys.argv:
        sys.exit(1 if report_credential_shapes() else 0)

    rotate_all()
    print()
    verify_current_key_only()
    print()
    report_credential_shapes()
