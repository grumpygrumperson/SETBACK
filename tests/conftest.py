"""
Test configuration.

metrics.py builds a Supabase client at import time, so importing it requires
SUPABASE_URL and SUPABASE_KEY to be set. Placeholders are installed here when
they're absent so the suite runs on a machine with no .env at all - a
contributor, or CI.

Nothing in the suite makes a network call: create_client() only constructs a
client, it doesn't connect. Real values in the environment are left alone.
"""

import os
import sys
from pathlib import Path

# Import the modules under test from the repo root, not from an installed copy
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SUPABASE_URL", "https://placeholder.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "placeholder-key-for-tests")

# A throwaway cipher key for the encryption round-trip tests. Set BEFORE the
# modules import: load_dotenv() does not override variables that already
# exist, so this keeps the suite off the real FERNET_KEY even on a machine
# that has one.
if not os.environ.get("FERNET_KEY"):
    from cryptography.fernet import Fernet
    os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
