# AURA — Digital Visibility Optimization Platform

**AURA** (internal brand: *AI Unified Reach Assistant*) is a fourth-year
engineering major project: an AI-powered search-visibility and
content-intelligence platform for small businesses.
**Analyze. Optimize. Publish. Grow.**

A business signs up, registers its website, and AURA crawls it, scores its SEO
and its *estimated AI-search readiness*, recommends prioritized fixes,
generates/optimizes content with Gemini, publishes or schedules it through
connectors, tracks analytics with explicit provenance, and proves improvement
over time by re-scanning. Group IB13: Aadi Patil, Shaurya Tiwari, Raj Sharma,
Suraj Salvi.

## How it works (the spine)

```text
Business → Website → Crawl → SEO + AI-visibility scoring
  → Recommendations → Content opportunities → Generate → Optimize
  → Edit → Approve ("Mark ready") → Schedule / Publish → Analytics → Re-scan
```

Key properties:

- **Deterministic engines, no fake numbers.** The crawler, the 27-rule SEO
  engine, the 23-signal AI-visibility engine, and the impact×effort
  recommendation engine are pure application logic — see `METHODOLOGY.md` for
  the versioned scoring rubrics. Gemini only *generates, rewrites, explains,
  summarizes, and chats*; it is never imported into scoring or crawling.
- **Honest demo mode.** Where external credentials are absent, connectors and
  analytics run in explicitly labelled demo mode (DB `CHECK` constraints make
  simulated-as-real rows unforgeable). Every analytics value carries provenance:
  `platform_api`, `demo`, `user_entered`, or `derived`.
- **Estimated AI-search readiness is a heuristic.** It is stored with a
  per-row disclaimer and is never presented as a ranking or citation guarantee.

## Prerequisites

- **Python 3.13** (backend), **Node.js 20** (frontend)
- Optional: Docker + Docker Compose (for the PostgreSQL deployment)
- Optional: a free Gemini API key ([AI Studio](https://aistudio.google.com/app/apikey))
  — content generation and the assistant are disabled without it, but crawling,
  scoring, and recommendations all work
- Optional: Supabase project — local auth works with zero external accounts

## Local development

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp ../.env.example .env            # then fill in SECRET_KEY etc.
```

Run migrations, then start the API (the project ships no `alembic.ini` on
purpose — the database URL must never live in a tracked file — so migrations
run programmatically):

```bash
python -c "from alembic import command; from alembic.config import Config; c = Config(); c.set_main_option('script_location', 'alembic'); command.upgrade(c, 'head')"
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs. Without `DATABASE_URL` the backend falls
back to a local SQLite file (`backend/aura.db`), so the full feature set runs
with no database server.

### Frontend

```bash
cd frontend
npm install
cp ../.env.example .env.local      # then set NEXT_PUBLIC_API_BASE_URL etc.
npm run dev
```

Open http://localhost:3000. Styling is hand-rolled CSS (`app/globals.css`) —
Tailwind/shadcn/Recharts were considered but deliberately not adopted
mid-rework; see CHANGELOG. The frontend stores a short-lived access token and a
rotating refresh token; on a 401 it silently refreshes once, and concurrent
401s share a single in-flight refresh.

## Docker (PostgreSQL deployment)

```powershell
# Windows (PowerShell) — from the project root:
copy .env.example .env      # then set POSTGRES_PASSWORD, SECRET_KEY, etc. in .env
copy .env backend\.env      # the api container reads backend/.env; the root .env
                            # is only used by Compose for variable interpolation
docker compose up --build
```
```bash
# macOS / Linux — from the project root:
cp .env.example .env        # then set POSTGRES_PASSWORD, SECRET_KEY, etc. in .env
cp .env backend/.env        # the api container reads backend/.env; the root .env
                            # is only used by Compose for variable interpolation
docker compose up --build
```

The backend container waits for Postgres, runs `alembic upgrade head`
(`backend/docker-entrypoint.py`), then starts uvicorn with 2 workers. Scheduler
leadership across workers uses a PostgreSQL advisory lock, so only one process
runs APScheduler jobs. Smoke-tested on 2026-09-26 (Docker Desktop on Windows):
two packaging bugs found and fixed since — a bad `public/` copy in
`frontend/Dockerfile` and the missing `backend/.env` step documented above.
For Supabase auth, also set `NEXT_PUBLIC_SUPABASE_URL` /
`NEXT_PUBLIC_SUPABASE_ANON_KEY` in the **root** `.env` (they are baked into the
frontend at build time via Compose build args) and the `SUPABASE_*` values in
`backend/.env`.

## Environment variables

See [.env.example](./.env.example) — it documents every variable the backend
reads (core, database, auth, Gemini, crawler limits, token encryption,
publishing connectors, scheduler, rate limiting) plus the frontend's
`NEXT_PUBLIC_*` variables. Copy it to `backend/.env` and `frontend/.env.local`;
never commit a real `.env`. Highlights:

- `AUTH_PROVIDER=local|supabase` — local needs nothing external; supabase needs
  `SUPABASE_URL` + `SUPABASE_ANON_KEY` (newer Supabase projects may call this
  the *publishable* key; `SUPABASE_PUBLISHABLE_KEY` is accepted as an alias).
  Token verification follows the project's signing mode automatically: newer
  projects using JWT Signing Keys (ES256) verify against the project's JWKS
  endpoint; legacy HS256 projects additionally need `SUPABASE_JWT_SECRET`.
- `GEMINI_MODEL=gemini-2.5-flash` (project decision; configurable via env).
- Crawler caps: `CRAWLER_MAX_PAGES=20`, `CRAWLER_MAX_DEPTH=3`,
  `CRAWLER_MAX_LINK_CHECKS=50`; `CRAWLER_ALLOW_PRIVATE_HOSTS` must stay
  `false` outside tests (it is the SSRF guard).
- Rate limiting (slowapi): `RATE_LIMIT_ENABLED`, `RATE_LIMIT_AUTH_PER_MINUTE=20`,
  `RATE_LIMIT_ANALYSIS_PER_HOUR=20`, `RATE_LIMIT_AI_PER_HOUR=60`, plus a global
  per-minute backstop.
- `GET /api/v1/meta/capabilities` reports exactly which features are live on a
  running installation and which credential would enable the rest.

## Verification — the one command

All executable proofs (the 12 `backend/scripts/verify_*.py` suites, ~178
checks) plus the pytest regression suite run via one command:

```bash
cd backend
python scripts/run_all_checks.py
```

It exits 0 only if every script and the full pytest suite pass, and prints a
summary table (`--dry-run` lists the order without executing). Individual
suites can still be run directly, e.g. `python scripts/verify_seo.py` (see the
docstring of each script). `pytest -q` alone runs the 11-test regression
suite. A successful full run was last recorded on 2026-09-25 after the rework
(see CHANGELOG).

## Documentation map

- [METHODOLOGY.md](./METHODOLOGY.md) — versioned scoring rubrics
  (`DVO-SEO-SCORE-V2.0`, AI-visibility engine), the recommendation priority
  model, and the crawler contract. Read this before defending the viva.
- [CHANGELOG.md](./CHANGELOG.md) — what changed in the 2026-09-25 rework and
  what is still future work.
- [HANDOFF.md](./HANDOFF.md) / [IMPLEMENTATION_STATUS.md](./IMPLEMENTATION_STATUS.md) —
  current state snapshots.
- `backend/CHANGELOG.md` — backend-internal change notes.

## Honesty contract

- No button does nothing; nothing permanently hard-coded in production views.
- Hero/landing artwork is illustrative and labelled as such.
- Scores are heuristics. Demo data is labelled `demo`, never mixed into live.
- Secrets only in environment files — never in source, logs, or chat.
