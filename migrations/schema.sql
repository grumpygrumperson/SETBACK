-- ============================================================================
-- schema.sql — complete SET API database schema
--
-- Everything the sync reads and writes, in one file. Safe to run on a fresh
-- Supabase project, and safe to re-run on an existing one: every statement is
-- guarded, so it converges on the same schema either way.
--
-- Paste the whole file into the Supabase SQL editor and press Run. It is one
-- transaction: either every statement applies or none does, so a failure
-- halfway through cannot leave the database half-migrated. Re-running it is
-- always safe.
--
-- Eight tables and three views:
--   participants          one row per person
--   participant_api_keys  one row per venue (spot and perps use DIFFERENT keys)
--   trade_metrics         closed orders, tagged by venue
--   balance_snapshots     the equity curve
--   cash_flows            external deposits/withdrawals, so returns mean something
--   fetch_errors          per-credential sync failures
--   participant_scores    flow-adjusted Sharpe, what the ranking uses
--   pending_signups       the signup form inbox, drained by sign_ups.py
--   latest_balances       view: current equity, by venue
--   daily_balances        view: last snapshot per venue per day, for scoring
--   leaderboard           view: the ranking
-- ============================================================================


begin;


-- ---------------------------------------------------------------------------
-- 0. Legacy value repair
--
-- Runs before any constraint is added, because ADD CONSTRAINT revalidates
-- every existing row - and in a single-transaction file, one legacy row
-- failing a check would roll back the entire migration.
--
-- Two kinds of legacy value exist, and they are repaired rather than
-- rejected because both have exactly one correct answer:
--
--   account_type  Coinbase reports INTX perpetuals as product_type='FUTURE'
--                 with contract_expiry_type=null, so an older classifier
--                 stored them as 'future'. They are perpetuals; 'perp' is
--                 what they should have said.
--
--   exchange      Every row written before multi-venue support is Coinbase.
--                 It was the only venue that existed.
--
-- Anything that still does not fit afterwards is NOT guessed at. The final
-- block raises with the table and the offending values named, which is a far
-- more useful failure than PostgreSQL's bare 23514 with a constraint name.
-- ---------------------------------------------------------------------------

do $$
declare
  t  text;
  bad text;
begin
  -- The tables may not exist yet on a fresh project; skip what isn't there.
  foreach t in array array['participant_api_keys', 'trade_metrics',
                           'balance_snapshots', 'cash_flows']
  loop
    if to_regclass('public.' || t) is null then
      continue;
    end if;

    -- account_type: fold the known synonyms onto 'perp'
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = t
                  and column_name = 'account_type') then
      execute format(
        'update public.%I set account_type = %L
          where lower(account_type) in (%L, %L, %L, %L, %L)',
        t, 'perp', 'future', 'futures', 'swap', 'perpetual', 'perps');

      -- A null predates the column being populated at all, and everything
      -- that old is spot - Coinbase perps came later. The original file did
      -- this for trade_metrics further down; doing it here too means the
      -- check below cannot fire on a row the old file handled fine.
      execute format(
        'update public.%I set account_type = ''spot'' where account_type is null', t);

      execute format(
        'select string_agg(distinct coalesce(account_type, ''<null>''), '', '')
           from public.%I where account_type is null
              or account_type not in (''spot'', ''perp'')', t)
        into bad;

      if bad is not null then
        raise exception
          'public.% has account_type value(s) this schema cannot accept: %. '
          'Allowed values are ''spot'' and ''perp''. Fix or remove those rows, '
          'then run this file again. Nothing has been changed.', t, bad;
      end if;
    end if;

    -- exchange: everything older than multi-venue support is Coinbase
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = t
                  and column_name = 'exchange') then
      execute format(
        'update public.%I set exchange = ''coinbase'' where exchange is null', t);

      execute format(
        'select string_agg(distinct exchange, '', '')
           from public.%I where exchange not in (''coinbase'', ''lighter'')', t)
        into bad;

      if bad is not null then
        raise exception
          'public.% has exchange value(s) with no adapter in venues.py: %. '
          'Either add the adapter and list it here, or fix those rows. '
          'Nothing has been changed.', t, bad;
      end if;
    end if;
  end loop;
end $$;


-- ---------------------------------------------------------------------------
-- 1. Participants
--
-- Identity only. Split from credentials so a policy that exposes a display
-- name can't also expose live exchange keys.
-- ---------------------------------------------------------------------------

create table if not exists public.participants (
  id           uuid primary key default gen_random_uuid(),
  display_name text not null,
  email        text unique not null,
  created_at   timestamptz not null default now()
);


-- ---------------------------------------------------------------------------
-- 2. Credentials — one row per venue
--
-- Coinbase issues separate API keys for spot and perps, so a participant
-- trading both has two rows here sharing one participant_id. Values are
-- Fernet-encrypted by sign_ups.py; plaintext keys never reach the database.
--
-- portfolio_uuid is required for Coinbase perps (the INTX endpoints are
-- portfolio-scoped) and null for spot. Captured at signup by
-- find_perp_portfolio_uuid() so the sync never pays for that lookup.
-- ---------------------------------------------------------------------------

create table if not exists public.participant_api_keys (
  id             bigint generated always as identity primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  exchange       text not null,               -- ccxt id, e.g. 'coinbase'
  account_type   text not null,               -- 'spot' | 'perp'
  api_key        text not null,               -- Fernet encrypted
  api_secret     text,                        -- Fernet encrypted; null on
                                              -- single-credential venues
  api_passphrase text,                        -- Fernet encrypted; some venues only
  portfolio_uuid text,                        -- Coinbase: INTX portfolio UUID
                                              -- Lighter:  account index
  is_active      boolean not null default true,
  created_at     timestamptz not null default now(),

  constraint participant_api_keys_venue_key
    unique (participant_id, exchange, account_type),

  constraint participant_api_keys_account_type_check
    check (account_type in ('spot', 'perp'))
);

-- Bring an existing table up to spec. Everything below is a no-op on a fresh
-- database and the actual repair on an older one.
--
-- This block was missing, and its absence made the whole file fail on any
-- database created before multi-venue support: the inline column list above
-- only runs when the table does NOT already exist, so `exchange` was never
-- added to participant_api_keys the way it is to the other three tables - and
-- the check constraint further down then referenced a column that wasn't
-- there. Nothing caught it because a database created fresh from this file
-- already has the column.
alter table public.participant_api_keys add column if not exists exchange text;
update public.participant_api_keys set exchange = 'coinbase' where exchange is null;
alter table public.participant_api_keys alter column exchange set not null;

-- What the venue said this key can do, captured when it was proven read-only
-- at signup. Coinbase answers this via GET /api/v3/brokerage/key_permissions
-- ({can_view, can_trade, can_transfer, portfolio_uuid, portfolio_type});
-- Lighter has no equivalent because the read-only token format is itself the
-- guarantee, so its rows stay null.
--
-- Stored rather than re-derived because permissions CAN WIDEN after signup -
-- a participant can edit a key's scope on the exchange at any time, and
-- nothing notifies us. Keeping the last answer and its timestamp makes
-- re-checking a scheduled job over stale rows (~200 requests a day) instead
-- of a check on every sync (~4,800).
alter table public.participant_api_keys
  add column if not exists permissions jsonb;
alter table public.participant_api_keys
  add column if not exists permissions_checked_at timestamptz;

-- Same for the venue key. sign_ups.register_credential() upserts with
-- on_conflict=(participant_id, exchange, account_type), which needs a real
-- unique constraint to target - without it every registration fails with
-- "42P10: no unique or exclusion constraint matching the ON CONFLICT
-- specification".
--
-- Duplicates first, or the constraint refuses to build. Keeps the newest row
-- per venue: unlike the history tables, where the earliest row is the
-- original, a repeated credential row means the participant re-registered
-- and the LAST key they gave is the one that still works.
delete from public.participant_api_keys a
 using public.participant_api_keys b
 where a.id < b.id
   and a.participant_id = b.participant_id
   and a.exchange       = b.exchange
   and a.account_type   = b.account_type;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'participant_api_keys_venue_key'
  ) then
    alter table public.participant_api_keys
      add constraint participant_api_keys_venue_key
      unique (participant_id, exchange, account_type);
  end if;
end $$;


-- account_type is restricted to what the code actually HANDLES, not to every
-- venue Coinbase can name. get_account_totals_usdc() dispatches perp vs. spot
-- and nothing else; latest_balances sums only those two, so a 'future' row
-- would be silently absent from the leaderboard total.
--
-- The narrowness is the point. Coinbase reports INTX perpetuals as
-- product_type='FUTURE' with contract_expiry_type=null, so a naive classifier
-- stores them as 'future' - which then makes the resume-point lookup in
-- get_last_synced_timestamp(pid, 'perp') find nothing and re-fetch the entire
-- order history on every run. Silent, and permanent. A constraint that
-- ALLOWED 'future' would not have caught that; this one turns it into an
-- immediate insert error.
--
-- Inline constraints are a no-op on a table that already exists, so the
-- drop/add below is what actually repairs an older database. The add
-- revalidates every existing row, which is the intended behaviour: it should
-- refuse rather than accept rows the sync can't process.
alter table public.participant_api_keys
  drop constraint if exists participant_api_keys_account_type_check;
alter table public.participant_api_keys
  add constraint participant_api_keys_account_type_check
  check (account_type in ('spot', 'perp'));

-- Not every venue issues a key/secret PAIR. Coinbase does; Lighter
-- authenticates with a single value - a read-only token, or an L1 private
-- key - which is stored in api_key with nothing to put here. Forcing a
-- placeholder into a NOT NULL column would make the table lie about what it
-- holds, and the placeholder would eventually be handed to an exchange.
alter table public.participant_api_keys
  alter column api_secret drop not null;

-- `exchange` must name a venue the code actually has an adapter for -
-- venues.VENUES is the source of truth and this is the database agreeing
-- with it.
--
-- Without this, a typo at registration ('coinbse') inserts happily and then
-- fails EVERY sync with UnknownVenue, leaving that participant silently off
-- the leaderboard for as long as nobody reads fetch_errors. Worse on a
-- cross-exchange strategy: with one venue missing, the other leg reads as an
-- unhedged directional bet rather than half of a hedge.
--
-- The cost is that adding a third venue needs a line here as well as in
-- venues.py. tests/test_venues.py asserts the two lists match, so they can't
-- drift apart in silence.
alter table public.participant_api_keys
  drop constraint if exists participant_api_keys_exchange_check;
alter table public.participant_api_keys
  add constraint participant_api_keys_exchange_check
  check (exchange in ('coinbase', 'lighter'));


-- ---------------------------------------------------------------------------
-- 3. Trade metrics — closed orders
--
-- Order IDs are only unique PER VENUE, so the key is
-- (participant_id, exchange, account_type, order_id). Postgres also treats
-- NULLs as distinct, so both venue columns must be NOT NULL or duplicates
-- re-insert on every run.
--
-- `exchange` is not decoration. Coinbase INTX and Lighter are both 'perp',
-- so without it one participant's two venues are the same row to this table -
-- and get_last_synced_timestamp(pid, 'perp') would return the newest
-- timestamp across BOTH. Syncing Coinbase after Lighter had traded more
-- recently would resume from Lighter's last trade and skip every Coinbase
-- order in between. Resume points only move forward, so those orders would
-- never be fetched again.
-- ---------------------------------------------------------------------------

create table if not exists public.trade_metrics (
  id             bigint generated always as identity primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  exchange       text not null,               -- ccxt id: 'coinbase' | 'lighter'
  account_type   text not null,
  order_id       text not null,
  timestamp      bigint,                      -- exchange ms, ccxt convention
  datetime       timestamptz,
  symbol         text,
  type           text,                        -- 'limit' | 'market' | ...
  side           text,                        -- 'buy' | 'sell'
  price          numeric,
  amount         numeric,
  fee_cost       numeric,
  fee_currency   text,
  created_at     timestamptz not null default now()
);

-- Legacy constraints from the original single-venue design. A global unique
-- on order_id makes spot+perp impossible; the old FK pointed at a table that
-- no longer exists. Dropped here so re-running this file repairs an older DB.
alter table public.trade_metrics
  drop constraint if exists trade_metrics_order_id_key;
alter table public.trade_metrics
  drop constraint if exists trade_metrics_participant_order_key;

-- Bring an existing table up to spec (no-ops on a fresh one)
alter table public.trade_metrics add column if not exists account_type text;
update public.trade_metrics set account_type = 'spot' where account_type is null;
alter table public.trade_metrics alter column account_type set not null;

-- Every row that predates multi-venue support is Coinbase - it was the only
-- venue that existed. Backfilled before the NOT NULL so the alter succeeds.
alter table public.trade_metrics add column if not exists exchange text;
update public.trade_metrics set exchange = 'coinbase' where exchange is null;
alter table public.trade_metrics alter column exchange set not null;

-- The pre-venue key. Dropped so the wider one can replace it; leaving both
-- would reject a Lighter order that legitimately shares an order_id with a
-- Coinbase one.
alter table public.trade_metrics
  drop constraint if exists trade_metrics_participant_account_order_key;

-- Duplicates must go first, or the constraint refuses to build and takes the
-- whole file down with it. Keeps the lowest id of each group, matching what
-- balance_snapshots does below.
delete from public.trade_metrics a
 using public.trade_metrics b
 where a.id > b.id
   and a.participant_id = b.participant_id
   and a.exchange       = b.exchange
   and a.account_type   = b.account_type
   and a.order_id       = b.order_id;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'trade_metrics_participant_venue_order_key'
  ) then
    alter table public.trade_metrics
      add constraint trade_metrics_participant_venue_order_key
      unique (participant_id, exchange, account_type, order_id);
  end if;
end $$;

-- Same vocabulary as participant_api_keys. Orders are fetched with a
-- portfolio-scoped key, so sync_orders() stamps the CREDENTIAL's venue rather
-- than trusting the order's own product_type; this constraint is what makes a
-- regression back to the order-derived value fail loudly instead of quietly
-- orphaning the resume point.
alter table public.trade_metrics
  drop constraint if exists trade_metrics_account_type_check;
alter table public.trade_metrics
  add constraint trade_metrics_account_type_check
  check (account_type in ('spot', 'perp'));

-- Same vocabulary as participant_api_keys.exchange - see the note there.
alter table public.trade_metrics
  drop constraint if exists trade_metrics_exchange_check;
alter table public.trade_metrics
  add constraint trade_metrics_exchange_check
  check (exchange in ('coinbase', 'lighter'));


-- ---------------------------------------------------------------------------
-- 4. Balance snapshots — the equity curve
--
-- One row per credential per run. total_usdc is the headline figure for both
-- venues; detail keeps whatever else the valuation returned (per-wallet
-- totals for spot, collateral / unrealized PnL / notional for perps), since
-- none of it can be backfilled later.
-- ---------------------------------------------------------------------------

create table if not exists public.balance_snapshots (
  id             bigint generated always as identity primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  exchange       text not null,               -- ccxt id: 'coinbase' | 'lighter'
  account_type   text not null,
  timestamp      bigint not null,             -- exchange ms
  total_usdc     numeric,
  detail         jsonb,
  created_at     timestamptz not null default now(),

  constraint balance_snapshots_participant_venue_ts_key
    unique (participant_id, exchange, account_type, timestamp)
);

-- The constraint above is declared inline, which is a NO-OP on a table that
-- already exists - so a database created before it was added would never
-- acquire it, and sync_balance_snapshot's upsert would fail with
-- "42P10: no unique or exclusion constraint matching the ON CONFLICT
-- specification". Added explicitly here so re-running this file repairs it.
--
-- Backfill before the NOT NULL, as with trade_metrics.
alter table public.balance_snapshots add column if not exists exchange text;
update public.balance_snapshots set exchange = 'coinbase' where exchange is null;
alter table public.balance_snapshots alter column exchange set not null;

-- Duplicates must go first, or the constraint refuses to build.
delete from public.balance_snapshots a
 using public.balance_snapshots b
 where a.id > b.id
   and a.participant_id = b.participant_id
   and a.exchange       = b.exchange
   and a.account_type   = b.account_type
   and a.timestamp      = b.timestamp;

alter table public.balance_snapshots
  drop constraint if exists balance_snapshots_participant_account_ts_key;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'balance_snapshots_participant_venue_ts_key'
  ) then
    alter table public.balance_snapshots
      add constraint balance_snapshots_participant_venue_ts_key
      unique (participant_id, exchange, account_type, timestamp);
  end if;
end $$;

-- Matters most here: latest_balances sums only 'spot' and 'perp', so a
-- snapshot stored under any other venue would be dropped from the leaderboard
-- total without any error to say so.
alter table public.balance_snapshots
  drop constraint if exists balance_snapshots_account_type_check;
alter table public.balance_snapshots
  add constraint balance_snapshots_account_type_check
  check (account_type in ('spot', 'perp'));

-- Matters most here, for the same reason the account_type check does:
-- latest_balances sums per (exchange, account_type), so a snapshot written
-- under an unrecognised venue would still be summed into the leaderboard
-- while being invisible to every other query that knows the venue list.
alter table public.balance_snapshots
  drop constraint if exists balance_snapshots_exchange_check;
alter table public.balance_snapshots
  add constraint balance_snapshots_exchange_check
  check (exchange in ('coinbase', 'lighter'));


-- ---------------------------------------------------------------------------
-- 5. Fetch errors
--
-- Written by log_fetch_error(). Without this, a participant whose keys expire
-- mid-competition just stops appearing, with nothing but stdout to say why.
-- ---------------------------------------------------------------------------

create table if not exists public.fetch_errors (
  id             bigint generated always as identity primary key,
  participant_id uuid references public.participants(id) on delete cascade,
  error_message  text,
  created_at     timestamptz not null default now()
);


-- ---------------------------------------------------------------------------
-- 6. Cash flows — external deposits and withdrawals
--
-- Without this, a participant who deposits $1,000 mid-competition shows the
-- transfer as profit, and every return-based metric ranks funding rather
-- than trading.
--
-- usdc_value is SIGNED: deposits positive, withdrawals negative, valued at
-- the price when the transfer settled (not today's price).
--
-- Only genuinely EXTERNAL transfers reach this table. get_cash_flows() keeps
-- an allowlist of transfer types, so trades ('advanced_trade_fill') and moves
-- between a participant's own spot and perp portfolios ('intx_deposit') are
-- dropped at fetch time. Counting a trade as funding would subtract every
-- trade from that participant's return.
--
-- There is deliberately no is_internal column. Deciding whether a transfer is
-- internal needs a participant's venues compared against each other, which the
-- per-credential sync never sees - so it's computed at read time in
-- metrics.mark_internal_transfers(). A stored flag nothing populated would
-- read as authoritative while always saying 'false'.
-- ---------------------------------------------------------------------------

create table if not exists public.cash_flows (
  id             bigint generated always as identity primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
  exchange       text not null,               -- ccxt id: 'coinbase' | 'lighter'
  account_type   text not null,
  transfer_id    text not null,
  timestamp      bigint not null,             -- exchange ms
  datetime       timestamptz,
  direction      text not null,               -- 'in' | 'out'
  currency       text,
  amount         numeric,                     -- in `currency`
  usdc_value     numeric,                     -- signed, valued at transfer time
  raw_type       text,                        -- exchange's own label, for auditing
  created_at     timestamptz not null default now(),

  constraint cash_flows_participant_venue_transfer_key
    unique (participant_id, exchange, account_type, transfer_id),

  constraint cash_flows_direction_check
    check (direction in ('in', 'out'))
);

-- An earlier revision of this file carried an is_internal column that nothing
-- ever wrote. Dropped here so a database created from that version converges
-- on the same schema; no-op on a fresh one.
alter table public.cash_flows drop column if exists is_internal;

-- Backfill and re-key for multi-venue, as with the other two tables.
alter table public.cash_flows add column if not exists exchange text;
update public.cash_flows set exchange = 'coinbase' where exchange is null;
alter table public.cash_flows alter column exchange set not null;

alter table public.cash_flows
  drop constraint if exists cash_flows_participant_account_transfer_key;

-- Same reason as trade_metrics and balance_snapshots.
delete from public.cash_flows a
 using public.cash_flows b
 where a.id > b.id
   and a.participant_id = b.participant_id
   and a.exchange       = b.exchange
   and a.account_type   = b.account_type
   and a.transfer_id    = b.transfer_id;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'cash_flows_participant_venue_transfer_key'
  ) then
    alter table public.cash_flows
      add constraint cash_flows_participant_venue_transfer_key
      unique (participant_id, exchange, account_type, transfer_id);
  end if;
end $$;

-- Same vocabulary again. metrics.mark_internal_transfers() pairs a
-- participant's flows across their venues; a third venue label it doesn't
-- expect would leave those transfers unmatched and counted as external
-- funding, distorting every return.
alter table public.cash_flows
  drop constraint if exists cash_flows_account_type_check;
alter table public.cash_flows
  add constraint cash_flows_account_type_check
  check (account_type in ('spot', 'perp'));

-- Same vocabulary again. metrics.mark_internal_transfers() pairs a
-- participant's flows by (exchange, account_type) to net out money moved
-- between their own venues - central to cross-exchange arbitrage, where
-- collateral shuttles constantly. An unrecognised venue label would leave
-- those transfers unpaired and counted as real external funding.
alter table public.cash_flows
  drop constraint if exists cash_flows_exchange_check;
alter table public.cash_flows
  add constraint cash_flows_exchange_check
  check (exchange in ('coinbase', 'lighter'));


-- ---------------------------------------------------------------------------
-- 7. Participant scores — what the leaderboard actually ranks on
--
-- Written by score.py after every sync. This is the table that makes the
-- competition a competition: without it the only ranking available is
-- latest_balances.total_usdc, which ranks DEPOSITS. On live data the two
-- orderings came out exactly inverted - the participant with 2,000x more
-- money in the account had the worse risk-adjusted return.
--
-- One CURRENT row per (participant, period), upserted. Deliberately not a
-- history table: the score is recomputed from complete stored history on
-- every run, so a time series is derivable from balance_snapshots whenever
-- it is wanted, and storing 200 rows an hour that nothing reads is exactly
-- what the dropped is_internal column (section 6) argues against.
--
-- sharpe is NULLABLE and that is load-bearing. Three different situations
-- produce no number, and all three must still produce a ROW:
--
--   too little history   a new participant, or one whose credential broke
--                        before three snapshots existed
--   no volatility        a flat account - the guard in sharpe_from_returns
--   registration bug     a participants row whose credential write failed,
--                        which is invisible everywhere else in the system
--
-- A participant who is absent from this table is a participant nobody can
-- see is missing. unreliable_reason carries the explanation so the answer is
-- readable rather than just blank.
-- ---------------------------------------------------------------------------

create table if not exists public.participant_scores (
  participant_id     uuid not null references public.participants(id) on delete cascade,
  period             text not null,
  computed_at        timestamptz not null default now(),

  sharpe             numeric,          -- null when not computable; see above
  unreliable_reason  text,

  -- `reliable` is periods >= 20. For the first ~20 days of a 90-day
  -- competition NOBODY qualifies, so this sorts the board rather than
  -- filtering it - provisional entries rank below established ones instead
  -- of vanishing, which would look identical to not having registered.
  reliable           boolean not null default false,

  periods            integer,          -- single-period returns that survived gaps
  observed_periods   integer,
  gap_periods        integer,
  calendar_periods   integer,
  mean_return        numeric,
  volatility         numeric,

  -- Kept because they are the audit trail for the adjustment. A participant
  -- disputing their rank is really disputing one of these numbers.
  --
  -- The two venue figures are separate on purpose: they mean opposite things.
  -- dropped_venue_usdc is money that left the competition when a credential
  -- went silent for three days; joined_venue_usdc is a venue's opening
  -- balance the first time it reported, booked as an inflow so that
  -- registering a second exchange mid-competition does not read as profit.
  -- Netting them would hide both, and the join is the larger correction in
  -- practice - adding a venue is routine, losing one is not.
  external_flow_usdc numeric,
  dropped_venue_usdc numeric,
  joined_venue_usdc  numeric,
  internal_transfers integer,

  first_period       text,
  last_period        text,

  primary key (participant_id, period)
);

-- The column list above only runs when the table does NOT already exist, so
-- anything added to it later has to be repeated here or a database created
-- from an earlier revision never acquires it. That exact omission is what
-- made this file fail on any pre-multi-venue database once, when `exchange`
-- was added to participant_api_keys and only declared inline.
--
-- A missing column here would not error, which is worse: score.py's upsert
-- would be rejected by PostgREST for an unknown column and EVERY
-- participant's score would silently stop updating.
alter table public.participant_scores
  add column if not exists joined_venue_usdc numeric;

alter table public.participant_scores
  drop constraint if exists participant_scores_period_check;
alter table public.participant_scores
  add constraint participant_scores_period_check
  check (period in ('daily', 'hourly'));


-- ---------------------------------------------------------------------------
-- 8. Pending signups — the signup form inbox
--
-- The form POSTs here; sign_ups.py verifies each credential against the
-- exchange, re-encrypts it under FERNET_KEY into participant_api_keys, and
-- wipes the plaintext from this row.
--
-- READ THIS BEFORE CHANGING ANYTHING HERE.
--
-- Credentials sit in this table IN PLAINTEXT between submission and import.
-- That is a deliberate trade, not an oversight, and it has a cost worth
-- stating plainly: anyone who can read this table, and every Supabase backup
-- taken while a row is pending, holds usable exchange credentials.
--
-- The trade is defensible for two specific reasons, and stops being
-- defensible if either changes:
--
--   1. Every credential here is READ-ONLY - verify_credential rejects
--      anything that can trade or transfer. A leak exposes positions, not
--      funds. If this table is ever used for a credential that can move
--      money, this design is wrong.
--
--   2. The window is short and under the operator's control. Rows are
--      cleared on import, so running sign_ups.py often keeps the table
--      empty. A table left undrained for weeks is the failure mode.
--
-- The alternative - encrypting in the browser so this table never holds
-- plaintext - cannot be done with Fernet. Fernet is symmetric, so the page
-- would need FERNET_KEY, and shipping that to every visitor would expose the
-- key protecting every already-stored credential. Public-key encryption in
-- the browser does solve it, and a Supabase Edge Function holding FERNET_KEY
-- solves it too; both were judged more machinery than this is worth.
--
-- The credential columns are NULLABLE because they are wiped the moment a row
-- resolves. An imported one now lives Fernet-encrypted in
-- participant_api_keys; a rejected one is a credential nobody will use. The
-- row stays for the audit trail; the secret does not.
-- ---------------------------------------------------------------------------

create table if not exists public.pending_signups (
  id             bigint generated always as identity primary key,
  display_name   text not null,
  email          text not null,
  exchange       text not null,
  account_type   text not null,

  -- Plaintext, and only until the importer runs. See the warning above.
  api_key        text,
  api_secret     text,                    -- null on single-credential venues

  status         text not null default 'pending',
  attempts       integer not null default 0,
  last_error     text,
  submitted_at   timestamptz not null default now(),
  processed_at   timestamptz
);

-- Restated outside the CREATE for the usual reason: the column list above is
-- a no-op on a table that already exists, so anything added later has to
-- appear here too or a database built from an earlier revision never gets it.
alter table public.pending_signups add column if not exists api_key text;
alter table public.pending_signups add column if not exists api_secret text;
alter table public.pending_signups add column if not exists attempts integer not null default 0;
alter table public.pending_signups add column if not exists last_error text;
alter table public.pending_signups add column if not exists processed_at timestamptz;

-- Columns from earlier revisions of this file, dropped rather than left in
-- place: a column nothing writes and nothing reads is one somebody eventually
-- assumes is meaningful.
--
--   ciphertext      held a sealed envelope, before the browser-side
--                   encryption was traded away for simplicity.
--   api_passphrase  asked participants for something NEITHER venue uses -
--                   ccxt.coinbase requires only apiKey and secret, and
--                   ccxt.lighter only privateKey. It was stored, encrypted,
--                   and handed over as `password`, which coinbase ignores.
--                   participant_api_keys keeps its own passphrase column,
--                   because the venue contract still supports venues that
--                   need one; nothing reaching this table does.
alter table public.pending_signups drop column if exists ciphertext;
alter table public.pending_signups drop column if exists api_passphrase;

-- anon can INSERT into this table, which makes every constraint below a limit
-- on what an anonymous caller can store. The size bounds are the point:
-- without them one request could park an arbitrary amount of data here.
--
-- They are not a defence against VOLUME. Nothing in Postgres stops the same
-- caller submitting repeatedly - that belongs at the edge, in Supabase rate
-- limiting or a captcha in front of the form, and is worth adding before the
-- form is advertised anywhere public.
alter table public.pending_signups drop constraint if exists pending_signups_status_check;
alter table public.pending_signups add constraint pending_signups_status_check
  check (status in ('pending', 'imported', 'rejected'));

alter table public.pending_signups drop constraint if exists pending_signups_exchange_check;
alter table public.pending_signups add constraint pending_signups_exchange_check
  check (exchange in ('coinbase', 'lighter'));

alter table public.pending_signups drop constraint if exists pending_signups_account_type_check;
alter table public.pending_signups add constraint pending_signups_account_type_check
  check (account_type in ('spot', 'perp'));

-- Generous but finite. A Coinbase CDP secret is a PEM-encoded EC private key
-- of a few hundred characters, which is the longest thing legitimately
-- submitted here.
alter table public.pending_signups drop constraint if exists pending_signups_size_check;
alter table public.pending_signups add constraint pending_signups_size_check
  check (
    length(display_name) between 1 and 40
    and length(email) between 3 and 320
    and (api_key is null or length(api_key) <= 2000)
    and (api_secret is null or length(api_secret) <= 4000)
  );

-- A resolved row keeps no credential. Enforced in the database rather than
-- left to the importer, because this is the property that bounds how long
-- plaintext lives here - and a bug in Python should not be able to leave a
-- usable credential sitting in a table anon can write to.
alter table public.pending_signups drop constraint if exists pending_signups_resolved_is_empty;
alter table public.pending_signups add constraint pending_signups_resolved_is_empty
  check (
    status = 'pending'
    or (api_key is null and api_secret is null)
  );


-- ---------------------------------------------------------------------------
-- 9. Indexes
--
-- Two distinct access patterns, and an index that serves one will not serve
-- the other:
--
--   the SYNC     asks "what is the newest row for this credential", filtered
--                by venue, sorted descending, limit 1 — once per credential
--                per run, so 400 lookups every 15 minutes at 200 credentials.
--
--   the METRICS  read a participant's ENTIRE history, every venue, oldest
--                first, in pages. Unpaginated selects were silently capped by
--                PostgREST at Supabase's max-rows, which — because the sort is
--                oldest-first — kept the OLDEST rows and quietly froze every
--                participant's score in a window that stopped advancing.
-- ---------------------------------------------------------------------------

-- Column order matches the resume-point lookups exactly: they filter on
-- participant + exchange + account_type and take the newest timestamp.
drop index if exists trade_metrics_participant_time_idx;
create index if not exists trade_metrics_participant_venue_time_idx
  on public.trade_metrics (participant_id, exchange, account_type, timestamp desc);

drop index if exists balance_snapshots_participant_time_idx;
create index if not exists balance_snapshots_participant_venue_time_idx
  on public.balance_snapshots (participant_id, exchange, account_type, timestamp desc);

-- The importer only ever asks for unresolved rows, while resolved ones
-- accumulate for the audit trail - so the useful index is the partial one.
create index if not exists pending_signups_pending_idx
  on public.pending_signups (id) where status = 'pending';

-- The sync now reads every active credential in ONE request rather than one
-- per participant, ordered so that the same credential collects a
-- participant's account-wide transfers on every run. A partial index on the
-- is_active predicate is what keeps that a small scan rather than a full one.
drop index if exists participant_api_keys_participant_idx;
create index if not exists participant_api_keys_active_idx
  on public.participant_api_keys (exchange, account_type, id) where is_active;

-- Two different queries read cash_flows, and they want different orders.
--
-- 1. The resume point. For a venue whose transfer history covers the whole
--    ACCOUNT — Coinbase — the lookup deliberately does NOT filter on
--    account_type, because only one of a participant's credentials collects
--    those transfers and which one that is can change. So the filter is
--    (participant_id, exchange) and the answer is the newest timestamp.
create index if not exists cash_flows_participant_venue_time_idx
  on public.cash_flows (participant_id, exchange, timestamp desc);

-- 2. metrics.fetch_cash_flows() reads a participant's whole history oldest
--    first, paged. id is in the key because the sort is (timestamp, id):
--    two transfers in the same millisecond have no defined order otherwise,
--    and rows can shift between pages under an ambiguous sort — dropping one
--    and repeating another.
drop index if exists cash_flows_participant_time_idx;
create index if not exists cash_flows_participant_time_id_idx
  on public.cash_flows (participant_id, timestamp, id);

-- metrics.fetch_snapshots() has the same shape and the same reason. The
-- venue-keyed index above serves the resume-point lookup, which sorts
-- DESCENDING within a single venue; this one serves the metrics read, which
-- sorts ascending across all of them. Neither substitutes for the other.
--
-- This is the one that grows: the sync writes a snapshot per credential per
-- run, so at 200 credentials on a 15-minute cron it is ~19,000 rows a day.
create index if not exists balance_snapshots_participant_time_id_idx
  on public.balance_snapshots (participant_id, timestamp, id);


-- ---------------------------------------------------------------------------
-- 10. Leaderboard views
--
-- TWO views, and the distinction matters:
--
--   latest_balances  current equity per participant, by venue. Correct at
--                    what it does, and it was only ever MISLABELLED as the
--                    leaderboard - ranking on it ranks whoever deposited
--                    most. Kept as a display column.
--
--   leaderboard      the actual ranking, on flow-adjusted Sharpe from
--                    participant_scores.
--
-- Spot and perps are ONE competition, so total_usdc sums both venues while
-- keeping each visible separately.
--
-- The inner `distinct on` takes the latest snapshot PER (exchange,
-- account_type): a participant's venues are written seconds apart, so
-- grouping by participant alone and taking max(timestamp) would silently drop
-- all but one.
--
-- exchange belongs in that key as much as account_type does. Coinbase INTX
-- and Lighter are both 'perp', so keying on account_type alone would pick one
-- of them and discard the other - showing a fraction of the participant's
-- money on the leaderboard, with nothing to indicate anything was missing.
-- spot_usdc and perp_usdc therefore sum ACROSS venues.
--
-- left join keeps participants with no snapshots yet on the board at 0
-- rather than vanishing.
-- ---------------------------------------------------------------------------

create or replace view public.latest_balances as
select
  p.id                                                                 as participant_id,
  p.display_name,
  coalesce(sum(b.total_usdc) filter (where b.account_type = 'spot'), 0) as spot_usdc,
  coalesce(sum(b.total_usdc) filter (where b.account_type = 'perp'), 0) as perp_usdc,
  coalesce(sum(b.total_usdc), 0)                                       as total_usdc,
  max(b.timestamp)                                                     as as_of
from public.participants p
left join (
  select distinct on (participant_id, exchange, account_type)
         participant_id, exchange, account_type, total_usdc, timestamp
    from public.balance_snapshots
   order by participant_id, exchange, account_type, timestamp desc
) b on b.participant_id = p.id
group by p.id, p.display_name;


-- Daily equity: one row per venue per UTC day.
--
-- The scorer's read path. Not a summary for display - it is exactly what
-- build_portfolio_series already reduces balance_snapshots to for a daily
-- axis, computed in Postgres instead of shipped over the network and thrown
-- away in Python.
--
-- Why: score.py re-reads every participant's COMPLETE history on every run,
-- and a competition only gets longer. Measured growth at the current write
-- rate (54 rows per participant per day):
--
--     participants   rows @90d   MB/run   paged requests/run
--                2       9,643        2                   11
--              100     482,143       78                  487
--              200     964,286      155                  973
--
-- That is every hour, forever, rising linearly with elapsed days. Reading
-- last-of-day turns 200 participants into ~36,000 rows and ~44 requests.
--
-- It is EXACT, not approximate. build_portfolio_series takes the LAST
-- snapshot of each venue within each period, so on a daily axis every row
-- this view drops is one the metric already discarded. Verified against live
-- data: sharpe, mean_return, volatility and every period count identical
-- from 20 rows as from 405.
--
-- The alternative was deleting intermediate snapshots on a retention policy.
-- This is better: same read reduction, nothing destroyed, and reversible.
-- Full-resolution history stays for audit, for disputes, and for anything
-- that needs an intra-day axis.
--
-- `id` is carried because callers sort on (timestamp, id): two venues written
-- in the same millisecond have no defined order otherwise, and rows can shift
-- between pages under an ambiguous sort - dropping one and repeating another.
create or replace view public.daily_balances as
select distinct on (participant_id, exchange, account_type,
                    (to_timestamp(timestamp / 1000.0) at time zone 'UTC')::date)
       id,
       participant_id,
       exchange,
       account_type,
       timestamp,
       total_usdc
  from public.balance_snapshots
 order by participant_id, exchange, account_type,
          (to_timestamp(timestamp / 1000.0) at time zone 'UTC')::date,
          timestamp desc, id desc;


-- The competition ranking.
--
-- rank() is materialised as a COLUMN rather than left to an ORDER BY on the
-- view, because an ORDER BY inside a view is not guaranteed to survive an
-- outer query - a client doing `select * from leaderboard limit 10` could
-- silently get ten arbitrary rows. As a column the rank cannot be lost.
--
-- Sorted by `reliable` first so that early in the competition, when nobody
-- has the 20 daily returns that make a Sharpe meaningful, provisional
-- entries still appear - below the established ones, not instead of them.
--
-- nulls last on both keys keeps participants with no computable score at the
-- bottom rather than at the top, which is where NULL sorts by default in
-- descending order.
create or replace view public.leaderboard as
select
  p.id                                     as participant_id,
  p.display_name,
  s.sharpe,
  s.reliable,
  s.unreliable_reason,
  s.periods,
  s.gap_periods,
  -- The three numbers that were subtracted from this participant's return.
  -- All three or none: a disputed rank is really a dispute about one of
  -- them, and showing external_flow_usdc while hiding the venue adjustments
  -- would make that argument impossible to settle from the leaderboard.
  s.external_flow_usdc,
  s.joined_venue_usdc,
  s.dropped_venue_usdc,
  s.computed_at,
  b.spot_usdc,
  b.perp_usdc,
  b.total_usdc,
  b.as_of,
  rank() over (order by s.reliable desc nulls last,
                        s.sharpe   desc nulls last)  as rank
from public.participants p
left join public.participant_scores s
       on s.participant_id = p.id and s.period = 'daily'
left join public.latest_balances b
       on b.participant_id = p.id;


-- ---------------------------------------------------------------------------
-- 11. Row Level Security
--
-- The sync uses the SERVICE ROLE key, which bypasses RLS entirely. These
-- policies only constrain what the anon key can reach.
--
-- participant_api_keys gets RLS with NO policy on purpose: for a table of
-- live exchange credentials, "nobody with the anon key reads anything" is
-- the correct answer.
-- ---------------------------------------------------------------------------

alter table public.participants         enable row level security;
alter table public.participant_api_keys enable row level security;
alter table public.trade_metrics        enable row level security;
alter table public.balance_snapshots    enable row level security;
alter table public.fetch_errors         enable row level security;
alter table public.cash_flows           enable row level security;
alter table public.participant_scores   enable row level security;
alter table public.pending_signups      enable row level security;

-- The ONE place anon is allowed to write.
--
-- Insert and nothing else: there is deliberately no select policy, so a
-- submitter cannot read back their own row, cannot read anyone else's, and
-- cannot use the endpoint to test whether an email is already registered.
--
-- The grant is stated explicitly rather than relying on whatever Supabase
-- has granted on the public schema, and the other verbs are revoked, because
-- "anon can only insert" is the property this entire table depends on.
drop policy if exists "anon submits a signup" on public.pending_signups;
create policy "anon submits a signup"
  on public.pending_signups
  for insert
  to anon
  with check (true);

grant insert on public.pending_signups to anon;
revoke select, update, delete on public.pending_signups from anon;
revoke all on public.pending_signups from authenticated;


drop policy if exists "participants read own trades"    on public.trade_metrics;
drop policy if exists "participants read own snapshots" on public.balance_snapshots;
drop policy if exists "leaderboard is readable"         on public.participants;

create policy "participants read own trades"
  on public.trade_metrics for select
  using (participant_id = auth.uid());

create policy "participants read own snapshots"
  on public.balance_snapshots for select
  using (participant_id = auth.uid());

create policy "leaderboard is readable"
  on public.participants for select
  using (true);


-- ---------------------------------------------------------------------------
-- 12. View access
--
-- IMPORTANT: views do NOT inherit RLS. By default a view runs with its
-- owner's privileges, so latest_balances bypasses every policy above — which
-- is why Supabase labels it "Unrestricted".
--
-- Default below: logged-in participants see the leaderboard, the public
-- internet does not. To make it fully public instead, grant to anon as well.
-- ---------------------------------------------------------------------------

revoke all on public.latest_balances from anon;
grant select on public.latest_balances to authenticated;

-- The leaderboard carries no email, no key material and no per-trade detail -
-- only display_name and a score - so it is the one view that could safely be
-- made public. Granted to `authenticated` by default; add `anon` to turn it
-- into a public JSON endpoint over PostgREST with no frontend at all.
revoke all on public.leaderboard from anon;
grant select on public.leaderboard to authenticated;

-- daily_balances is revoked from BOTH roles, unlike the two views above.
-- Views bypass RLS, and this one exposes every participant's complete daily
-- equity curve - not just their current total. The only consumer is score.py,
-- which connects with the service role and is unaffected by grants. Nothing
-- else should be able to read it.
revoke all on public.daily_balances from anon;
revoke all on public.daily_balances from authenticated;


-- ---------------------------------------------------------------------------
-- 13. Confirmation
--
-- The SQL editor shows the result of the LAST statement, so this is what you
-- see after pressing Run. Without it a successful migration looks identical
-- to one that silently did nothing.
-- ---------------------------------------------------------------------------

commit;

select
  (select count(*) from information_schema.tables
    where table_schema = 'public'
      and table_name in ('participants', 'participant_api_keys',
                         'trade_metrics', 'balance_snapshots',
                         'fetch_errors', 'cash_flows'))          as tables,
  (select count(*) from pg_indexes
    where schemaname = 'public')                                 as indexes,
  (select count(*) from pg_constraint c
     join pg_class t on t.oid = c.conrelid
    where c.contype = 'c'
      and c.conname like '%\_check')                            as check_constraints,
  (select count(*) from information_schema.views
    where table_schema = 'public' and table_name = 'latest_balances')
                                                                 as leaderboard_view,
  (select count(*) from public.participants)                     as participants,
  (select count(*) from public.participant_api_keys where is_active)
                                                                 as active_credentials;
