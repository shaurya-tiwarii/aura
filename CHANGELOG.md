# AURA — Project Changelog

## 2026-09-26 — Supabase JWT Signing Keys (ES256) support

New Supabase projects sign user access tokens with asymmetric JWT Signing
Keys (ES256); the dashboard shows the legacy JWT secret as verify-only, so the
previous HS256-only verification would have rejected every Supabase login on a
new project.

### FIXED — auth (2026-09-26)

- `backend/app/auth/supabase.py`: `verify_token` now reads the token's `alg`
  header. ES256/RS256 tokens verify against the project's JWKS
  (`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`, key selected by `kid`,
  issuer pinned to `{SUPABASE_URL}/auth/v1`); HS256 tokens keep the existing
  offline shared-secret path. Unknown algorithms are rejected; an unreachable
  JWKS endpoint surfaces `token_verification_unavailable` instead of a
  misleading `token_invalid`.
- `backend/app/core/config.py`: `SUPABASE_JWT_SECRET` is no longer required for
  `AUTH_PROVIDER=supabase` (legacy HS256 projects only).
- `backend/scripts/verify_auth.py`: new offline check mints ES256 tokens with a
  throwaway P-256 key and a stubbed JWKS fetch (valid accepted; wrong issuer,
  unknown kid, and tampered signature rejected).
- `.env.example` / `README.md`: document that the JWT secret is legacy-only.


## 2026-09-26 — Smoke-test fixes (from first real `docker compose up --build`)

Two packaging defects surfaced on the first real Compose run (Docker Desktop,
Windows) and are fixed here:

### FIXED — packaging (2026-09-26)

- **`frontend/Dockerfile`: removed `COPY --from=builder /app/public ./public`.**
  The frontend has no `public/` directory, so the build failed with
  `/app/public: not found`. (Fixed locally during the smoke test; now in the
  package.)
- **Documented the `backend/.env` step for Docker.** The `api` service reads
  `backend/.env` (`env_file`, `required: false`); the root `.env` is only used
  for Compose interpolation — so without `copy .env backend\.env` the API
  failed production validation (`SECRET_KEY` not visible). README Docker
  section now shows the copy step for PowerShell and bash.
- **Supabase browser config wired through the Docker build.**
  `docker-compose.yml` passes `NEXT_PUBLIC_SUPABASE_URL` /
  `NEXT_PUBLIC_SUPABASE_ANON_KEY` as frontend build args (interpolated from the
  root `.env`); `frontend/Dockerfile` declares matching `ARG`/`ENV` so
  `next build` bakes them in. Empty values keep the local-auth-only build
  working. `.env.example` notes that these two values must live in the ROOT
  `.env` for Docker (not only `frontend/.env.local`).

## 2026-09-25 — Major rework (4 parallel workstreams)

Four workstreams ran against the frozen contracts: (A) competitor-tracking
removal, (B) backend fixes, (C) frontend fixes, (D) docs/packaging prep (this
document set). All `backend/scripts/verify_*.py` suites and the pytest suite
were green after the rework.

### REMOVED (2026-09-25)

- **Competitor tracking — per the user's explicit approval on 2026-09-25.**
  The user directed the removal; this was not a scope decision by the agent.
  Deleted: `backend/app/api/routes/competitors.py`,
  `backend/app/schemas/competitor.py`, `backend/app/models/competitor.py`,
  `backend/app/services/competitor.py`; the router mount and model imports;
  enums (`ScanTrigger.COMPETITOR`, `NotificationType.COMPETITOR_ANALYZED`,
  `ActivityAction.COMPETITOR_ADDED`/`COMPETITOR_ANALYZED`); the
  `ContentOpportunity.competitor_id` column; `generate_from_competitor_reports()`;
  the Competitors dashboard section + competitor CSS. Alembic revision
  `397896607f5c` drops the `competitors`/`competitor_reports` tables.
  Competitor content must not appear in docs, the spine, or the README.
- **Dead backend dependencies** (grep-confirmed zero imports anywhere):
  `python-dateutil`, `tenacity`, `markdown`, `bleach`, `freezegun`. Logged in
  `backend/CHANGELOG.md`.
- **Dead frontend references:** `NEXT_PUBLIC_AUTH_PROVIDER` docs (OAuth is
  gated on the presence of `NEXT_PUBLIC_SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_ANON_KEY`),
  and the `next lint` script.
- **HeroScene mock stats** (defaked): "+24.8% CRAWL SIGNAL" →
  "Signal field illustration"; "AI SEARCH READINESS 72" → "DECORATIVE
  ARTWORK / No real data shown"; "● LIVE MODEL" → "ILLUSTRATIVE ONLY".

### FIXED — backend (2026-09-25)

- `backend/docker-entrypoint.py`: waits for Postgres (up to 90 s) → runs
  `alembic upgrade head` → execs uvicorn with 2 workers.
- `docker-compose.yml`: backend/postgres sections fixed (frontend section
  untouched).
- **Scheduler leadership** via PostgreSQL advisory lock (fixed key
  `2026092501`) on a dedicated `NullPool` connection; SQLite assumes a single
  process. 5-minute reconciliation job re-registers pending scheduled rows.
- **Scheduled-post retries** with exponential backoff
  (`SCHEDULER_RETRY_BACKOFF_SECONDS`, default 300) and retry-budget
  notifications; exhausting the budget marks the row FAILED.
- **Publish-failure persistence:** a failed direct publish returns HTTP 200
  with a `status=failed` body and preserves the attempt record (no silent
  drop).
- **Rate limiting (slowapi) attached:** auth 20/min, scans/content/AI
  per-hour, global per-minute backstop; 429s returned in the standard error
  envelope.
- **`broken_links` rule implemented for real:** `SafeFetcher.probe` (HEAD +
  bodyless-GET fallback, SSRF guards), ≤50 external URLs per scan
  (`CRAWLER_MAX_LINK_CHECKS`).
- **Crawler page cap canonicalized to 20** (`CRAWLER_MAX_PAGES=20`, stale "25"
  comments fixed).
- **`GEMINI_MODEL` env var, default `gemini-2.5-flash`** (was inconsistently
  `3.6-flash`).
- `SUPABASE_PUBLISHABLE_KEY` added as a deprecated alias for
  `SUPABASE_ANON_KEY`.
- Notifications/activity events reduced to the spine: scan complete, content
  generated, publish success/failure, scheduled execution, score improvement.
- Fixed the pre-existing SQLite-incompatible migration and the verify scripts.

### FIXED — frontend (2026-09-25)

- `frontend/Dockerfile` (multi-stage `node:20-alpine`, Next.js standalone
  output) + `.dockerignore`.
- Restored landing/login/HeroScene CSS in `globals.css` (all 49 className
  usages verified covered; mint-on-near-black theme, no Tailwind).
- Scheduled-posts panel now reads real `GET /businesses/{id}/publishing/scheduled`.
- Schedule-modal misfire fixed (separate `schedulingContent` state).
- **Refresh-token flow:** one silent refresh on 401; both tokens replaced per
  the rotation scheme; concurrent 401s share one in-flight promise.
- TS2322 on `onMonitoringSave` fixed; Badge tones whitelisted to real CSS
  classes; `tsc` 0 errors; `next build` succeeds.
- **Analytics provenance labels on every value** (`platform_api` / `demo` /
  `user_entered` / `derived`, from the backend `source_breakdown`).
- New **"Mark ready" approve step** (`PATCH content/{id}` `{status:"ready"}`),
  completing the spine:
  recommendation → opportunity → generate → optimize → edit → **approve** →
  schedule/publish → measure.
- Assistant chat kept (genuinely works against
  `POST /businesses/{id}/assistant/chat`).

### ADDED (2026-09-25)

- `backend/scripts/run_all_checks.py` — the single documented validation
  command (`cd backend && python scripts/run_all_checks.py`): runs all 12
  `verify_*.py` suites + `pytest -q`, prints a summary table, exits non-zero
  on any failure (`--dry-run` lists the order).
- `METHODOLOGY.md` — versioned scoring rubrics for the viva:
  **DVO-SEO-SCORE-V2.0** (27 rules, 8 weighted categories) and the 23-signal
  AI-visibility engine, explicitly superseding the frozen 10-check V1 draft;
  the impact×effort recommendation model; the crawler contract.
- `README.md` rewritten as an accurate quickstart (venv + uvicorn, frontend
  npm, docker compose, the one verification command, env setup pointer).
- `.env.example` now notes the frontend's `NEXT_PUBLIC_API_URL` →
  `NEXT_PUBLIC_API_BASE_URL` precedence.
- New root `CHANGELOG.md` (this file).

### FUTURE WORK (not done — deliberately deferred or blocked)

- **Tailwind/shadcn migration:** deliberately not adopted mid-rework. The
  frontend uses hand-rolled CSS in `globals.css` and every className is
  verified against it. Adopting Tailwind would be a redesign task, not a fix.
- **Real OAuth credentials for live connectors:** demo mode stands in,
  honestly labelled (demo/live CHECK constraints enforced). Real
  WordPress/LinkedIn/Reddit credentials are the user's to provision.
- **Live Docker Compose smoke test:** Docker was not available in this
  environment, so `docker compose up` has never been run end-to-end. The
  entrypoint, Dockerfiles, and health checks are scaffolding until then.
- **Advisory-lock failover against real Postgres:** the failover logic is
  reasoned through and documented (`backend/CHANGELOG.md`) but was not
  exercised against a live multi-worker Postgres deployment.
- ~~**`engine_version` V2 naming**~~ — done 2026-09-25: code now stamps
  `DVO-SEO-SCORE-V2.0` (SEO) and `DVO-AI-VIS-V2.0` (AI-visibility); METHODOLOGY.md
  updated to match.
- No-fake-functionality audit of every clickable feature (final pass).
- Gemini `AQ.`-format key rotation/validation against the live API.

## Earlier history

For backend-internal notes predating this rework, see `backend/CHANGELOG.md`.
