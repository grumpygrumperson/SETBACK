import { createClient } from "@supabase/supabase-js";

/**
 * One row of the `leaderboard` view (migrations/schema.sql).
 *
 * The three *_usdc adjustment figures are carried together on purpose: the
 * view's comment notes that a disputed rank is really a dispute about one of
 * them, so showing external_flow without the venue adjustments would make
 * that argument impossible to settle from the board.
 */
export type LeaderboardRow = {
  participant_id: string;
  display_name: string;
  rank: number;

  // null until score.py has enough history to compute one.
  sharpe: number | null;
  reliable: boolean | null;
  unreliable_reason: string | null;
  periods: number | null;
  gap_periods: number | null;

  external_flow_usdc: number | null;
  joined_venue_usdc: number | null;
  dropped_venue_usdc: number | null;

  spot_usdc: number | null;
  perp_usdc: number | null;
  total_usdc: number | null;

  computed_at: string | null;  // timestamptz, ISO string
  as_of: number | null;        // balance_snapshots.timestamp — epoch MILLISECONDS
};

export type LeaderboardResult = {
  rows: LeaderboardRow[];
  source: "live" | "mock";
  /** Why we fell back to mock data. Rendered in a banner, not swallowed. */
  notice: string | null;
};

/**
 * PostgREST serialises `numeric` inconsistently across versions — sometimes a
 * JSON number, sometimes a string to avoid float precision loss. Coerce once
 * here so no component has to care which it got.
 */
function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalise(raw: Record<string, unknown>): LeaderboardRow {
  return {
    participant_id: String(raw.participant_id ?? ""),
    display_name: String(raw.display_name ?? "—"),
    rank: num(raw.rank) ?? 0,
    sharpe: num(raw.sharpe),
    reliable: raw.reliable === null || raw.reliable === undefined ? null : Boolean(raw.reliable),
    unreliable_reason: raw.unreliable_reason ? String(raw.unreliable_reason) : null,
    periods: num(raw.periods),
    gap_periods: num(raw.gap_periods),
    external_flow_usdc: num(raw.external_flow_usdc),
    joined_venue_usdc: num(raw.joined_venue_usdc),
    dropped_venue_usdc: num(raw.dropped_venue_usdc),
    spot_usdc: num(raw.spot_usdc),
    perp_usdc: num(raw.perp_usdc),
    total_usdc: num(raw.total_usdc),
    computed_at: raw.computed_at ? String(raw.computed_at) : null,
    as_of: num(raw.as_of),
  };
}

/**
 * Read the board, degrading to sample data rather than to an error page.
 *
 * Two failures are expected during setup and both are reported in the UI
 * rather than thrown:
 *
 *   - env vars unset, i.e. the Vercel project has no Supabase credentials yet;
 *   - a permission error, because schema.sql ends with
 *     `revoke all on public.leaderboard from anon`. Until someone grants
 *     select to anon (or we add auth) the anon key legitimately sees nothing.
 */
export async function getLeaderboard(): Promise<LeaderboardResult> {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !key) {
    return {
      rows: MOCK_ROWS,
      source: "mock",
      notice:
        "No Supabase credentials configured. Showing sample data. " +
        "Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY.",
    };
  }

  const supabase = createClient(url, key, { auth: { persistSession: false } });

  // The view materialises `rank` as a column precisely so an outer query
  // cannot lose it, but it does NOT guarantee row order — so sort explicitly.
  const { data, error } = await supabase
    .from("leaderboard")
    .select("*")
    .order("rank", { ascending: true });

  if (error) {
    return {
      rows: MOCK_ROWS,
      source: "mock",
      notice:
        `Supabase returned an error, so this is sample data: ${error.message}. ` +
        "If it mentions permission, the leaderboard view is still revoked from anon.",
    };
  }

  return {
    rows: (data ?? []).map((row) => normalise(row as Record<string, unknown>)),
    source: "live",
    notice: null,
  };
}

/**
 * Sample data, shaped exactly like the view so the layout is exercised
 * honestly: an unreliable entry, a null Sharpe, and a participant with no
 * snapshots yet all appear on a real board and each renders differently.
 */
const HOUR = 3_600_000;
const now = Date.UTC(2026, 8, 3, 12, 0, 0);

export const MOCK_ROWS: LeaderboardRow[] = [
  {
    participant_id: "00000000-0000-0000-0000-000000000001",
    display_name: "vega_hunter",
    rank: 1,
    sharpe: 2.83, reliable: true, unreliable_reason: null, periods: 41, gap_periods: 0,
    external_flow_usdc: 0, joined_venue_usdc: 0, dropped_venue_usdc: 0,
    spot_usdc: 18420.55, perp_usdc: 46180.02, total_usdc: 64600.57,
    computed_at: new Date(now).toISOString(), as_of: now - HOUR,
  },
  {
    participant_id: "00000000-0000-0000-0000-000000000002",
    display_name: "cronjob",
    rank: 2,
    sharpe: 1.94, reliable: true, unreliable_reason: null, periods: 39, gap_periods: 2,
    external_flow_usdc: 25000, joined_venue_usdc: 12000, dropped_venue_usdc: 0,
    spot_usdc: 5310.9, perp_usdc: 88240.13, total_usdc: 93551.03,
    computed_at: new Date(now).toISOString(), as_of: now - HOUR,
  },
  {
    participant_id: "00000000-0000-0000-0000-000000000003",
    display_name: "delta_one",
    rank: 3,
    sharpe: 0.41, reliable: true, unreliable_reason: null, periods: 33, gap_periods: 5,
    external_flow_usdc: -8000, joined_venue_usdc: 0, dropped_venue_usdc: 4200,
    spot_usdc: 22150.4, perp_usdc: 0, total_usdc: 22150.4,
    computed_at: new Date(now).toISOString(), as_of: now - 2 * HOUR,
  },
  {
    participant_id: "00000000-0000-0000-0000-000000000004",
    display_name: "late_entry",
    rank: 4,
    sharpe: 3.62, reliable: false, unreliable_reason: "only 6 return periods",
    periods: 6, gap_periods: 0,
    external_flow_usdc: 0, joined_venue_usdc: 0, dropped_venue_usdc: 0,
    spot_usdc: 9900, perp_usdc: 1200.75, total_usdc: 11100.75,
    computed_at: new Date(now).toISOString(), as_of: now - HOUR,
  },
  {
    participant_id: "00000000-0000-0000-0000-000000000005",
    display_name: "just_registered",
    rank: 5,
    sharpe: null, reliable: false, unreliable_reason: "no snapshots yet",
    periods: null, gap_periods: null,
    external_flow_usdc: null, joined_venue_usdc: null, dropped_venue_usdc: null,
    spot_usdc: 0, perp_usdc: 0, total_usdc: 0,
    computed_at: null, as_of: null,
  },
];
