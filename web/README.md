# web

Frontend for the SET trading competition. Next.js App Router, deployed to
Vercel with **Root Directory set to `web/`** — the repository root stays a
Python project and keeps building on Railway.

```bash
npm install
cp .env.example .env.local   # optional; without it the board shows sample data
npm run dev
```

Without Supabase credentials the leaderboard renders sample rows and says so in
a banner, so the page is never broken — see `getLeaderboard()` in
`lib/leaderboard.ts`.

## Environment

| Variable | Notes |
|---|---|
| `NEXT_PUBLIC_SUPABASE_URL` | Project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | The **anon** key. Public by design; RLS constrains it. |

Never set `SUPABASE_KEY` (service_role) or `FERNET_KEY` here. They decrypt
every participant's exchange credentials and belong only to the backend.

## Before the live board works

`migrations/schema.sql` ends with `revoke all on public.leaderboard from anon`,
so the anon key cannot read the view yet. Either grant it —

```sql
grant select on public.leaderboard to anon;
```

— which the schema's own comments call safe, since the view carries no email
and no key material, or add Supabase Auth and keep it to `authenticated`.
