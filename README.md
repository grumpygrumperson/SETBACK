# SET API

Backend for a crypto trading competition. Participants register **read-only**
exchange credentials for Coinbase and Lighter; a scheduled job records their
equity, orders and cash flows into Supabase; everyone is ranked on a
**flow-adjusted Sharpe ratio** — so the leaderboard measures trading, not
deposit size.

---

## How it works

```
participant_api_keys  (Fernet-encrypted credentials in Supabase)
        │
        │  post_to_supabase.py, hourly
        ▼
  decrypt ──► ccxt ──► balances, orders, transfers
        │
        ▼
balance_snapshots · trade_metrics · cash_flows · fetch_errors
        │
        │  score.py, immediately after each sync
        ▼
  metrics.py  ──►  participant_scores  ──►  leaderboard view
```

Three properties the design turns on:

- **Deposits must not look like profit.** A participant who wires in $50k on
  day 30 has not earned a 50k return. `cash_flows` is recorded so returns can
  be computed time-weighted, with external flows divided out.
- **Every credential is independent.** One participant's revoked key must not
  cost anyone else their run, so each is fetched and written under its own
  error boundary.
- **A snapshot missed is gone forever.** Orders and cash flows resume from
  their stored timestamps next run; the equity curve point for 12:15 cannot be
  backfilled. That asymmetry decides what counts as a failure and what the
  exit code means.

---

## Quick start

```bash
uv sync --dev                 # install, including test dependencies
cp .env.example .env          # then fill in SUPABASE_URL, SUPABASE_KEY, FERNET_KEY
uv run pytest -q              # 308 tests, no network needed
```

Apply the schema by pasting **`migrations/schema.sql`** into the Supabase SQL
editor. The file is idempotent and transactional — safe to re-run, and it
repairs an existing database rather than only creating a fresh one.

---

## Environment

Every variable is documented in [`.env.example`](.env.example), which
`tests/test_venues.py` asserts stays in sync with the code in both directions.
The three that must be set:

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_KEY` | `service_role` key — the sync writes, and RLS blocks `anon` |
| `FERNET_KEY` | Decrypts stored participant credentials |

Two that must **never** be set on the deployed service: `ALLOW_WALLET_KEYS`
and `COINBASE_PERMISSIONS_OPTIONAL`. Both disable a read-only guarantee. See
the warnings in `.env.example`.

Locally these come from `.env` via `load_dotenv()`. On Railway there is no
`.env` file — Railway injects the variables into the process before Python
starts, `load_dotenv()` finds nothing, and `os.getenv()` reads what Railway
already put there.

---

## Database

| Object | What it holds |
|---|---|
| `participants` | One row per person |
| `participant_api_keys` | Encrypted credentials, one per venue |
| `balance_snapshots` | The equity curve — the irreplaceable table |
| `trade_metrics` | Closed orders |
| `cash_flows` | Deposits and withdrawals, so returns mean something |
| `fetch_errors` | Per-credential sync failures |
| `participant_scores` | Current Sharpe per participant, upserted each run |
| `pending_signups` | The signup form inbox — plaintext, wiped on import |
| `latest_balances` | *view* — current equity by venue |
| `daily_balances` | *view* — last snapshot per venue per day; what scoring reads |
| `leaderboard` | *view* — the ranking |

`leaderboard` sorts on `reliable desc, sharpe desc`. `reliable` means at least
20 return periods, so early in a competition nobody qualifies — provisional
entries sort below established ones rather than disappearing.

---

## Commands

```bash
uv run python post_to_supabase.py     # the hourly job: sync, then score
uv run python score.py                # rescore from stored history, no fetching
uv run python score.py --period daily
uv run python sign_ups.py             # drain pending signups from the form
uv run python sign_ups.py --csv       # legacy: import participant_signups.csv
uv run python metrics.py              # print Sharpe for everyone
uv run python rotate_credentials.py --audit   # report credential SHAPES only
uv run python coinbase.py             # sanity-check the adapter against your own keys
uv run python lighter.py
```

`post_to_supabase.py` exits non-zero only when the failure **rate** reaches
`SYNC_FAILURE_THRESHOLD` (default 1.0 — total failure). One participant's dead
key is not an outage, and an exit code that fires on any error stops meaning
anything. Scoring failures never change the exit code: the score recomputes
from complete history next run, so a failure there is stale ranks, not lost
data.

---

## Participant signup

Participants submit through a static form that POSTs to `pending_signups`.
The importer verifies each key against the exchange, encrypts it under
`FERNET_KEY`, and wipes the plaintext.

```
form  →  pending_signups        anon may INSERT, and nothing else
      →  sign_ups.py            verify read-only against the exchange
                                → Fernet-encrypt → participant_api_keys
                                → wipe the plaintext row
```

**One submission registers every venue.** The page asks for Coinbase spot,
Coinbase perpetuals and Lighter together, and POSTs an array of one to three
rows — one per venue filled in. Each section is optional; at least one is
required. Keeping a row per venue rather than one wide row means each
credential is verified independently, so a bad Coinbase key does not cost
someone their valid Lighter registration.

| Venue | What the participant pastes |
|---|---|
| Coinbase spot | Key name (`organizations/…/apiKeys/…`) **and** the EC private key |
| Coinbase perps | A *separate* key — Coinbase issues different credentials for perps |
| Lighter | The `ro:` read-only token, and nothing else |

No passphrase is asked for: `ccxt.coinbase` requires only `apiKey` and
`secret`, and `ccxt.lighter` only `privateKey`. Coinbase's INTX portfolio UUID
is resolved automatically at verification, so participants never supply it.

**Setting it up**

1. Fill in the two values in `CONFIG` at the bottom of
   [`signup_form.html`](signup_form.html): your Supabase URL and your **anon**
   key. Neither is secret — the anon key is public by design, and RLS is what
   constrains it.
2. Host the file anywhere static, over **HTTPS**. It can be embedded in a
   Figma site, served from a Supabase Storage bucket, GitHub Pages, or
   Netlify. Serve it over HTTPS because it asks people to type API keys.
3. Run `uv run python sign_ups.py` to drain the inbox.

**Drain it often.** Credentials sit in `pending_signups` **in plaintext**
between submission and import — anyone who can read that table, and every
Supabase backup taken while a row is pending, holds usable credentials. That
window is the whole exposure and it is under your control.

The trade is deliberate and rests on two things. Every credential here is
read-only, so a leak exposes positions rather than funds; and the rows clear
on import. If either stops being true — a credential that can move money, or a
table left undrained for weeks — this design needs revisiting. The alternative
is a Supabase Edge Function holding `FERNET_KEY` and encrypting at insert, so
plaintext never lands at all.

**Why not encrypt in the browser?** Fernet is symmetric. The page would need
`FERNET_KEY` itself, and shipping that to every visitor would expose the key
protecting every credential already stored.

**What the anon key can do.** Insert one row into `pending_signups`. It has no
`select` policy, so a submitter can't read their row back, can't read anyone
else's, and can't use the endpoint to probe whether an email is registered.
Verified against real PostgreSQL, along with a check constraint that refuses
to let a resolved row keep a credential — so the plaintext is destroyed on
import even if the importer has a bug.

**What it doesn't do.** Nothing rate-limits submissions. Sizes are bounded, but
volume isn't; put Supabase's rate limiting or a captcha in front of the form
before advertising it publicly.

Verification is asynchronous, so someone who submits a trade-capable key finds
out later. The reason is stored in `pending_signups.last_error`, and transient
failures are retried up to `MAX_IMPORT_ATTEMPTS` before the row is rejected and
its credential wiped.

---

## Deployment

Railway, `railpack.json` → `python post_to_supabase.py`, on a cron schedule.
Set the environment variables in the service's Variables tab.

The build runs `uv sync --locked`, which **refuses to resolve** and fails the
build when `uv.lock` disagrees with `pyproject.toml`. After changing
dependencies, run `uv lock` and commit the result. CI checks this.

Snapshot cadence matters to the metric: `build_portfolio_series` writes a
silent venue off after 3 stale periods, so the cron interval and that
threshold have to be read together.

---

## Rotating `FERNET_KEY`

The one procedure where wrong ordering is unrecoverable. `MultiFernet`
encrypts with the first key and decrypts with any, which is what makes this
survivable at all.

1. Generate a new key:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
2. Put the **new** key in `FERNET_KEY`, and move the **old** one to
   `FERNET_KEYS_RETIRED`. Both must be set at once — this is the step that
   keeps existing rows readable.
3. Run `uv run python rotate_credentials.py` to re-encrypt every row.
4. Only when it reports every row on the current key, remove
   `FERNET_KEYS_RETIRED`.

Skipping step 2 orphans every stored credential permanently: nothing decrypts,
and every participant must issue new exchange keys.

---

## Read-only enforcement

Credentials are rejected at registration unless they are provably read-only.

- **Coinbase** — `v3PrivateGetBrokerageKeyPermissions` must report `can_view`
  without `can_trade` or `can_transfer`. An error reading permissions is a
  rejection, not a pass.
- **Lighter** — only `ro:` read-only tokens are accepted. An L1 wallet private
  key is unscoped and irrevocable, controlling every asset in the wallet on
  every chain; it is refused at signup and again at build time.

`rotate_credentials.py --audit` reports the shape of every stored credential —
`participant_id` and shape only, never a value, not even a prefix.

---

## Tests and CI

```bash
uv run pytest -q          # no network: conftest.py installs placeholder secrets
uv run ruff check .
```

CI runs both against a **fresh checkout**, plus a production-only install
(`uv sync --locked --no-dev`) that imports every module. That last job exists
because two failures reached `main` that a local `pytest` could not catch:
a module referencing a constant that was never committed, and a source file
that was never committed at all. Only a clean checkout reproduces either.
