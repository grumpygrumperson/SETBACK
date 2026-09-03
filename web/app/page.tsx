import { getLeaderboard, type LeaderboardRow } from "@/lib/leaderboard";

// The sync runs hourly, so anything fresher than this is wasted work.
export const revalidate = 300;

const COMPETITION_START = "2026-09-01";
const COMPETITION_END = "2026-10-26";

function usd(value: number | null): string {
  if (value === null) return "—";
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

function sharpe(value: number | null): string {
  return value === null ? "—" : value.toFixed(2);
}

/**
 * Absolute UTC, never "3 hours ago". A relative string rendered on the server
 * freezes at build time and then quietly lies for the rest of the day.
 */
function stamp(ms: number | null): string {
  if (ms === null) return "never";
  return new Date(ms).toISOString().replace("T", " ").slice(0, 16) + "Z";
}

function Row({ row }: { row: LeaderboardRow }) {
  const provisional = row.reliable === false;

  return (
    <tr className="border-b border-white/5 transition-colors hover:bg-white/[0.03]">
      <td className="py-3 pl-4 pr-2 tabular-nums text-white/40">{row.rank}</td>

      <td className="px-2 py-3">
        <span className="font-medium text-white">{row.display_name}</span>
        {provisional && (
          <span
            className="ml-2 rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-amber-400/90"
            title={row.unreliable_reason ?? "fewer than 20 return periods"}
          >
            provisional
          </span>
        )}
      </td>

      <td
        className={`px-2 py-3 text-right font-mono tabular-nums ${
          row.sharpe === null
            ? "text-white/30"
            : row.sharpe >= 0
              ? "text-emerald-400"
              : "text-rose-400"
        }`}
      >
        {sharpe(row.sharpe)}
      </td>

      <td className="px-2 py-3 text-right font-mono tabular-nums text-white/50">
        {row.periods ?? "—"}
        {row.gap_periods ? (
          <span className="ml-1 text-white/25" title={`${row.gap_periods} gap periods`}>
            /{row.gap_periods}
          </span>
        ) : null}
      </td>

      <td className="px-2 py-3 text-right font-mono tabular-nums text-white/80">
        {usd(row.total_usdc)}
      </td>
      <td className="hidden px-2 py-3 text-right font-mono tabular-nums text-white/45 sm:table-cell">
        {usd(row.spot_usdc)}
      </td>
      <td className="hidden px-2 py-3 text-right font-mono tabular-nums text-white/45 sm:table-cell">
        {usd(row.perp_usdc)}
      </td>
      <td className="hidden py-3 pl-2 pr-4 text-right font-mono text-xs text-white/30 lg:table-cell">
        {stamp(row.as_of)}
      </td>
    </tr>
  );
}

export default async function Page() {
  const { rows, source, notice } = await getLeaderboard();

  return (
    <main className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-6 sm:py-16">
      <header className="mb-8">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight text-white">
            Leaderboard
          </h1>
          <span className="font-mono text-xs text-white/35">
            {COMPETITION_START} → {COMPETITION_END}
          </span>
        </div>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-white/45">
          Ranked on a flow-adjusted Sharpe ratio: deposits and withdrawals are
          divided out, so this measures trading rather than deposit size.
          Entries with fewer than 20 return periods are marked{" "}
          <span className="text-amber-400/80">provisional</span> and sort below
          established ones.
        </p>
      </header>

      {notice && (
        <div className="mb-6 rounded-md border border-amber-500/25 bg-amber-500/[0.07] px-4 py-3 text-sm text-amber-200/85">
          <span className="font-medium">Sample data.</span> {notice}
        </div>
      )}

      <div className="overflow-x-auto rounded-lg border border-white/10 bg-white/[0.02]">
        <table className="w-full min-w-[560px] border-collapse text-sm">
          <thead>
            <tr className="border-b border-white/10 text-left text-[11px] uppercase tracking-wider text-white/35">
              <th className="py-2.5 pl-4 pr-2 font-medium">#</th>
              <th className="px-2 py-2.5 font-medium">Participant</th>
              <th className="px-2 py-2.5 text-right font-medium">Sharpe</th>
              <th className="px-2 py-2.5 text-right font-medium" title="Return periods / gap periods">
                Periods
              </th>
              <th className="px-2 py-2.5 text-right font-medium">Equity</th>
              <th className="hidden px-2 py-2.5 text-right font-medium sm:table-cell">Spot</th>
              <th className="hidden px-2 py-2.5 text-right font-medium sm:table-cell">Perp</th>
              <th className="hidden py-2.5 pl-2 pr-4 text-right font-medium lg:table-cell">
                As of
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="px-4 py-12 text-center text-white/35">
                  Nobody has registered yet.
                </td>
              </tr>
            ) : (
              rows.map((row) => <Row key={row.participant_id} row={row} />)
            )}
          </tbody>
        </table>
      </div>

      <footer className="mt-6 flex flex-wrap items-center justify-between gap-2 font-mono text-xs text-white/25">
        <span>
          {rows.length} participant{rows.length === 1 ? "" : "s"}
        </span>
        <span>{source === "live" ? "live · supabase" : "sample data"}</span>
      </footer>
    </main>
  );
}
