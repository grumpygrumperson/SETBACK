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
    rotate_all()
    print()
    verify_current_key_only()
