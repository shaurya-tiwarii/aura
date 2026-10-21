# AURA — Context Handoff

> A self-contained brief for resuming AURA in a fresh chat (any AI or human).
> Current state reference: `README.md` (quickstart), `CHANGELOG.md` (the
> 2026-09-25 rework), `METHODOLOGY.md` (versioned scoring rubrics).
> Snapshot date: **2026-09-25**, post-rework.

## What AURA is

**AURA — Digital Visibility Optimization Platform** (internal brand: *AI
Unified Reach Assistant*). Tagline: *Analyze. Optimize. Publish. Grow.*
Fourth-year engineering major project, Group IB13 (Aadi Patil, Shaurya Tiwari,
Raj Sharma, Suraj Salvi). The bar is a **complete, working application** —
not a prototype, mockup, or proof of concept.

A small business signs up, registers its website, and AURA crawls it, scores
its SEO and its "Estimated AI-search readiness", recommends prioritized
fixes, generates/optimizes content with Gemini, edits/approves it
("Mark ready"), publishes or schedules it through connectors, tracks
analytics with explicit provenance, answers questions via an assistant, and
proves improvement over time by re-scanning.

**Competitor tracking was removed on 2026-09-25 per the user's explicit
approval.** It must not reappear in docs, code, or UI.

## The 10 non-negotiable rules (honesty + safety — these override convenience)

1. **No fake functionality.** No dead buttons, no permanently hard-coded
   dashboards. Never claim a real integration/analytics/ranking that isn't
   implemented. Where an external API is unavailable: build the real
   architecture + service interface, provide a **clearly-labelled demo mode**,
   document the gap.
2. **Never fabricate real-world metrics.** Every analytics number is labelled
   `platform_api` (real), `demo`, `user_entered`, or `derived`.
3. **The AI Visibility Score is a heuristic.** Call it "Estimated AI-search
   readiness"; never a guarantee of ChatGPT/Gemini/Perplexity/Google ranking.
   The disclaimer is stored per report row.
4. **Real engineering, not a Gemini wrapper.** Crawler, parser, SEO scoring,
   AI-visibility scoring, recommendation engine, connector abstraction,
   scheduling, analytics normalization, and improvement engine are genuine app
   logic. Gemini *only* generates, rewrites, explains, summarizes, chats.
5. **Zero-cost.** Free/open-source only; the core runs locally with no paid
   service.
6. **Secrets in env only.** Never hard-code/log/expose keys. Never ask the
   user to paste secrets into chat.
7. **Crawler safety.** Same-domain limits, page cap 20, depth 3, timeouts,
   politeness delays, dedup, robots.txt, SSRF guard against
   private/loopback hosts.
8. **Treat Gemini output as untrusted.** Validate all JSON; AI never executes
   commands or reaches another user's data.
9. **Every score is explainable.** Documented factors/weights/thresholds
   (METHODOLOGY.md); each deduction traces to a `rule_id`. No
   random/unexplained numbers.
10. **Don't reduce scope** without explicit user approval.

## Tech stack

- **Backend:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.0, Alembic.
  Modular monolith (explicitly *not* microservices).
- **DB:** PostgreSQL primary (psycopg v3); SQLite fallback for zero-setup
  local dev + in-memory tests. Both must keep working.
- **Auth:** local provider (Argon2id + PyJWT) *and* Supabase (JWT verify),
  behind one interface, selected by `AUTH_PROVIDER`. Refresh tokens rotate;
  frontend silently refreshes once per 401.
- **AI:** Gemini 2.5 Flash (`GEMINI_MODEL` env, default `gemini-2.5-flash`),
  structured JSON, validated.
- **Scheduling:** APScheduler, database as source of truth (jobs
  re-registered on restart); multi-worker leadership via a PostgreSQL
  advisory lock; scheduled-post retries with exponential backoff.
- **Frontend:** Next.js 15, React 19, TypeScript, **hand-rolled CSS**
  (`app/globals.css`) — Tailwind/shadcn/Recharts deliberately not adopted
  mid-rework. Multi-stage `frontend/Dockerfile` (standalone output).
- **Rate limiting:** slowapi — auth 20/min, scans/content/AI per-hour, global
  per-minute backstop.

## Build state — DONE and verified (2026-09-25 rework)

- Backend fixes: docker entrypoint (wait-for-Postgres → migrations →
  uvicorn), scheduler advisory lock + reconciliation, scheduled-post retries,
  publish-failure persistence, rate limiting, real `broken_links` probing
  (`CRAWLER_MAX_LINK_CHECKS=50`), crawler cap canonicalized to 20, dead deps
  removed, spine-only notifications/activity. All verify scripts + pytest
  green (pytest: 11 passed).
- Frontend fixes: `frontend/Dockerfile`, real scheduled-posts panel,
  schedule-modal fix, refresh-token flow, de-faked HeroScene, TS2322 fix,
  Badge tones, analytics provenance labels everywhere, "Mark ready" approve
  step, `tsc` 0 errors, `next build` success.
- Competitor tracking removed (user-approved): code, migrations, enums,
  dashboard section, CSS all gone.
- Scoring contracts: **DVO-SEO-SCORE-V2.0** (27 rules, 8 weighted categories)
  and the 23-signal AI-visibility engine supersede the frozen 10-check V1
  draft. See `METHODOLOGY.md`.

## Data model (structural honesty)

~30 tables. `mode` (live/demo) on connections/posts/scheduled_posts,
`source` on analytics rows; DB **CHECK constraints** make simulated-as-real
unforgeable. Scans immutable and sequence-numbered; the improvement loop
diffs two scans and credits a recommendation only when its specific `rule_id`
issue disappears. `engine_version` on every report (currently persists
`"1.0"`; the V2 names live in `METHODOLOGY.md` — reconcile in future work).

## What's next

- No-fake-functionality audit of every clickable feature.
- First real `docker compose up` smoke test (never run — no Docker available).
- Real OAuth credentials for live connectors (demo mode stands in, honestly
  labelled) + Gemini key rotation/validation.
- Advisory-lock failover against a real multi-worker Postgres deployment.
- Rename stored `engine_version` values to the V2 names in METHODOLOGY.md.

## How to run / verify

```bash
cd backend
python scripts/run_all_checks.py   # the one documented validation command
```

Local dev: `uvicorn app.main:app --reload` from `backend/` (no `run.py` in
this tree); migrations via `docker-entrypoint.py` or the programmatic
`alembic.command.upgrade` one-liner in README.md. Frontend: `npm install &&
npm run dev` from `frontend/`. Secrets only in `backend/.env` (git-ignored);
`.env.example` documents each variable.
