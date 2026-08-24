-- ============================================================================
-- schema.sql — complete SET API database schema
--
-- Everything the sync reads and writes, in one file. Safe to run on a fresh
-- Supabase project, and safe to re-run on an existing one: every statement is
-- guarded, so it converges on the same schema either way.
--
-- Paste the whole file into the Supabase SQL editor and run it.
--
-- Six tables and one view:
--   participants          one row per person
--   participant_api_keys  one row per venue (spot and perps use DIFFERENT keys)
--   trade_metrics         closed orders, tagged by venue
--   balance_snapshots     the equity curve
--   cash_flows            external deposits/withdrawals, so returns mean something
--   fetch_errors          per-credential sync failures
--   latest_balances       leaderboard: spot + perp + combined
-- ============================================================================


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
  api_secret     text not null,               -- Fernet encrypted
  api_passphrase text,                        -- Fernet encrypted; some venues only
  portfolio_uuid text,                        -- Coinbase perps (INTX)
  is_active      boolean not null default true,
  created_at     timestamptz not null default now(),

  constraint participant_api_keys_venue_key
    unique (participant_id, exchange, account_type),

  constraint participant_api_keys_account_type_check
    check (account_type in ('spot', 'perp'))
);

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


-- ---------------------------------------------------------------------------
-- 3. Trade metrics — closed orders
--
-- Order IDs are only unique PER VENUE, so the key includes account_type. With
-- order_id alone, a spot and a perp order sharing an ID would overwrite each
-- other; Postgres also treats NULLs as distinct, so account_type must be NOT
-- NULL or duplicates re-insert on every run.
-- ---------------------------------------------------------------------------

create table if not exists public.trade_metrics (
  id             bigint generated always as identity primary key,
  participant_id uuid not null references public.participants(id) on delete cascade,
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

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'trade_metrics_participant_account_order_key'
  ) then
    alter table public.trade_metrics
      add constraint trade_metrics_participant_account_order_key
      unique (participant_id, account_type, order_id);
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
  account_type   text not null,
  timestamp      bigint not null,             -- exchange ms
  total_usdc     numeric,
  detail         jsonb,
  created_at     timestamptz not null default now(),

  constraint balance_snapshots_participant_account_ts_key
    unique (participant_id, account_type, timestamp)
);

-- The constraint above is declared inline, which is a NO-OP on a table that
-- already exists - so a database created before it was added would never
-- acquire it, and sync_balance_snapshot's upsert would fail with
-- "42P10: no unique or exclusion constraint matching the ON CONFLICT
-- specification". Added explicitly here so re-running this file repairs it.
--
-- Duplicates must go first, or the constraint refuses to build.
delete from public.balance_snapshots a
 using public.balance_snapshots b
 where a.id > b.id
   and a.participant_id = b.participant_id
   and a.account_type   = b.account_type
   and a.timestamp      = b.timestamp;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'balance_snapshots_participant_account_ts_key'
  ) then
    alter table public.balance_snapshots
      add constraint balance_snapshots_participant_account_ts_key
      unique (participant_id, account_type, timestamp);
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

  constraint cash_flows_participant_account_transfer_key
    unique (participant_id, account_type, transfer_id),

  constraint cash_flows_direction_check
    check (direction in ('in', 'out'))
);

-- An earlier revision of this file carried an is_internal column that nothing
-- ever wrote. Dropped here so a database created from that version converges
-- on the same schema; no-op on a fresh one.
alter table public.cash_flows drop column if exists is_internal;

-- Same vocabulary again. metrics.mark_internal_transfers() pairs a
-- participant's flows across their venues; a third venue label it doesn't
-- expect would leave those transfers unmatched and counted as external
-- funding, distorting every return.
alter table public.cash_flows
  drop constraint if exists cash_flows_account_type_check;
alter table public.cash_flows
  add constraint cash_flows_account_type_check
  check (account_type in ('spot', 'perp'));


-- ---------------------------------------------------------------------------
-- 7. Indexes
--
-- get_last_synced_timestamp() runs once per participant per credential per
-- run — 200 lookups every 15 minutes at 100 participants.
-- ---------------------------------------------------------------------------

create index if not exists trade_metrics_participant_time_idx
  on public.trade_metrics (participant_id, account_type, timestamp desc);

create index if not exists balance_snapshots_participant_time_idx
  on public.balance_snapshots (participant_id, account_type, timestamp desc);

create index if not exists participant_api_keys_participant_idx
  on public.participant_api_keys (participant_id) where is_active;

create index if not exists cash_flows_participant_time_idx
  on public.cash_flows (participant_id, timestamp);


-- ---------------------------------------------------------------------------
-- 8. Leaderboard view
--
-- Spot and perps are ONE competition, so total_usdc sums both venues while
-- keeping each visible separately.
--
-- The inner `distinct on` takes the latest snapshot PER VENUE: spot and perp
-- are written seconds apart, so grouping by participant alone and taking
-- max(timestamp) would silently drop one of them.
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
  select distinct on (participant_id, account_type)
         participant_id, account_type, total_usdc, timestamp
    from public.balance_snapshots
   order by participant_id, account_type, timestamp desc
) b on b.participant_id = p.id
group by p.id, p.display_name;


-- ---------------------------------------------------------------------------
-- 9. Row Level Security
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
-- 10. View access
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
