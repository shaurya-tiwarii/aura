# AURA — Project Guide for Codex

**AURA: An AI-Powered Search Visibility, Content Intelligence, and Marketing
Automation Platform for Small Businesses.** Tagline: *Analyze. Optimize.
Publish. Grow.* This is a fourth-year engineering major project. The goal is a
**complete, working application** — not a prototype, mockup, or proof of
concept.

This file is the source of truth for anyone (human or AI) resuming work. It was
ported from a previous session so context is not lost across tools.

---

---

> **Update note — 2026-09-25.** This guide was ported from an August 2026
> session and the sections below are stale in places. Current reality:
> **the frontend exists** (Next.js 15 / React 19 / TypeScript with hand-rolled
> CSS in `app/globals.css` — Tailwind/shadcn/Recharts were deliberately **not**
> adopted mid-rework); **competitor tracking was removed** on 2026-09-25 per
> the user's explicit approval (no `competitors` module, routes, or dashboard
> section remains); **Docker scaffolding is complete** (backend entrypoint +
> `docker-compose.yml`, multi-stage `frontend/Dockerfile` — but the compose
> deployment has never been smoke-tested, no Docker was available); the backend
> and frontend went through a full fix pass (rate limiting, scheduler advisory
> lock, publish/schedule retry handling, refresh-token flow, analytics
> provenance, "Mark ready" approve step, HeroScene de-faked). Stage 7+ is
> done. For the current state, read **`README.md`** (quickstart),
> **`CHANGELOG.md`** (what changed in the 2026-09-25 rework),
> **`METHODOLOGY.md`** (the versioned scoring rubrics — DVO-SEO-SCORE-V2.0
> supersedes the frozen 10-check V1 draft), and **`backend/CHANGELOG.md`**.
> The 10 non-negotiable rules in this file still apply.

## Non-negotiable rules (from the spec)

These are honesty and safety constraints. They override convenience. Most are
enforced structurally in the data model, not just by convention.

1. **No fake functionality (§46).** No buttons that do nothing. No permanently
   hard-coded dashboards. Never claim an API integration, real analytics, or
   real AI-search ranking that isn't actually implemented. Where an external API
   is unavailable: implement the architecture and service interface, provide a
   **clearly-labelled demo mode**, and document the limitation.
2. **Never fabricate real-world metrics (§16/§25).** Every analytics number is
   labelled `platform_api` (real), `demo`, `user_entered`, or `derived`. A demo
   number must always be visibly a demo number.
3. **The AI Visibility Score is a heuristic (§7/§45).** Never claim it guarantees
   ChatGPT / Gemini / Perplexity / Google ranking or citation. Call it
   "Estimated AI-search readiness." The disclaimer is stored per report row.
4. **Real engineering, not a Gemini wrapper (§36/§37).** The crawler, HTML
   parser, SEO scoring, AI-visibility scoring, recommendation engine, connector
   abstraction, scheduling, analytics normalization, and improvement engine are
   all genuine application logic. Gemini only does generation, rewriting,
   explanation, summarization, and chat.
5. **Zero-cost (§8/§43).** Free/open-source only. The core must run locally with
   no paid service.
6. **Secrets (§20/§30/§31).** Never hard-code API keys — use env vars. Never log
   or expose keys, OAuth secrets, passwords, or internal stack traces.
   **Never ask the user to paste secrets into chat; have them write keys into
   `backend/.env` directly.**
7. **Crawler safety (§21).** Same-domain restriction, crawl limits, timeouts,
   request delays, dedup, error handling. SSRF guard against private/loopback
   hosts. Do not build an aggressive crawler.
8. **Treat Gemini output as untrusted (§22).** Validate all JSON. AI must not
   execute arbitrary commands or access another user's data.
9. **Every score is explainable (§24).** Document factors, weights, thresholds.
   No random or unexplained scores; each deduction traces to a `rule_id`.
10. **Don't reduce scope (§57).** Don't drop major modules or leave core
    functionality as TODO placeholders without explicit approval.

---

## Tech stack

- **Backend:** Python, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.0
  (`Mapped`/`mapped_column`), Alembic.
- **Database:** PostgreSQL primary (psycopg v3 driver). SQLite fallback for
  zero-setup local dev and in-memory tests.
- **Auth:** Supabase Auth (JWT verification) **+** a local dev provider
  (Argon2id + PyJWT), behind one interface, selected by `AUTH_PROVIDER`.
- **AI:** Gemini 2.5 Flash via `google-genai`, structured JSON output, validated.
- **Scheduling:** APScheduler with the **database as source of truth** (jobs
  re-registered on restart); multi-worker scheduler leadership via a
  PostgreSQL advisory lock.
- **Frontend (built):** Next.js 15, React 19, TypeScript, **hand-rolled CSS** in
  `app/globals.css` (Tailwind/shadcn/Recharts were deliberately not adopted
  mid-rework — see root `CHANGELOG.md`), standalone `frontend/Dockerfile`.
- Modular monolith — explicitly NOT microservices.

## Credentials available

- **Gemini API key** (enables generation/rewrites/chat; scoring and crawling
  are LLM-free regardless). A **Supabase project exists**; local auth still
  needs nothing external. No WordPress/LinkedIn/Reddit OAuth apps yet, so
  connectors run in **clearly-labelled demo mode** until real credentials are
  added. See `GET /api/v1/meta/capabilities` on a running server for what is
  live in a given installation.

---

## Build state

### Done — backend foundation + full data model (WRITTEN, not yet executed)

- Root: `.gitignore`, `.env.example` (every var documented), `backend/requirements.txt`.
- `backend/app/core/`: `config.py` (pydantic-settings; `capability_report()`
  drives the real-vs-demo UI), `logging.py` (structlog with automatic secret
  redaction at any depth), `errors.py` (AuraError hierarchy, no stack traces to
  clients), `database.py` (Postgres primary + SQLite fallback, SQLite FK pragma),
  `middleware.py` (request id, security headers), `security.py` (Argon2id, Fernet
  TokenVault).
- `backend/app/models/`: 30 tables across `base`, `enums`, `user`, `business`,
  `website`, `seo`, `ai_visibility`, `recommendation`, `content`, `publishing`,
  `analytics`, `competitor`, `assistant`, `system`, `improvement` + `__init__.py`
  (imports every model — load-bearing for SQLAlchemy relationship resolution and
  Alembic autogenerate). `base.py` also holds `UTCDateTime`, the type that keeps
  timestamps timezone-aware on both backends — see Stage 2 bug 6.

Key design decisions worth preserving:
- `mode` (live/demo) on connections/posts/scheduled_posts + `source` on analytics
  rows. CHECK constraints `live_connection_requires_token` and
  `published_live_post_requires_remote_id` make simulated-as-real unforgeable.
- Scans are immutable and sequence-numbered; the improvement loop diffs two scans
  and credits a recommendation only when its specific `rule_id` issue disappears.
- `engine_version` on every report; improvement records set `is_comparable=false`
  across version changes rather than reporting a misleading delta.

### ⚠️ FIRST THING TO DO: verify the foundation actually runs

**DONE — 2026-08-21. Foundation verified; all checks pass.** See
[Verification scripts](#verification-scripts) below for the current commands.

`verify_foundation.py` goes well beyond `create_all()`, because SQLAlchemy
resolves relationship strings lazily and cross-file typos survive a plain
`create_all()`: it runs `configure_mappers()`, compiles all 58 relationship join
conditions, checks all 42 FK targets resolve, compiles the DDL for
`postgresql+psycopg` with no server present, does a full ORM round-trip with
cascade-delete, and asserts the honesty CHECK constraints actually *reject* a
forged `mode="live"` row at the database level.

Three real bugs were found and fixed during verification (all invisible until
executed):

1. **`config.py` crashed on startup whenever a `.env` file existed.**
   pydantic-settings JSON-decodes `list[str]` fields at the *source* level,
   before `mode="before"` validators run, so the documented comma-separated
   `BACKEND_CORS_ORIGINS` raised `JSONDecodeError`. Fixed with
   `Annotated[list[str], NoDecode]`; the validator now owns all parsing and
   accepts both the comma and JSON-array forms.
2. **`logging.py` would have leaked the Gemini key.** Inline-secret patterns
   only covered the legacy `AIza…` shape; current Google AI Studio keys use
   `AQ.…` and passed through unredacted in free-text messages. Pattern added,
   plus a check asserting the *actually configured* key cannot appear in a log
   event.
3. **`alembic/env.py` half-applied migrations on SQLite** — see Stage 1 below.

### Stage 1 — DONE: Alembic migrations

`alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, and
`alembic/versions/20260821_1746_cba9c04b0950_initial_schema.py` (29 tables, 46
CHECK constraints). Verified: `upgrade head` → `alembic check` (no drift) →
`downgrade base` (no orphan tables) → `upgrade head` again, plus offline
PostgreSQL DDL compilation.

Four things in `env.py` are load-bearing and were each a bug first:

- **Explicit `connection.commit()`.** Alembic treats SQLite as
  non-transactional DDL, so `begin_transaction()` is a no-op there — but
  pysqlite only autocommits DDL, not the `INSERT INTO alembic_version` that
  records the revision. Result was a half-success: all 29 tables created,
  revision unrecorded, `alembic current` empty, and the next `upgrade head`
  trying to re-create existing tables. No-op on Postgres.
- **`render_item` preserving the JSON/JSONB variant.** Autogenerate flattens
  `JSON().with_variant(JSONB, "postgresql")` to `sa.JSON()`, which would
  silently downgrade 48 Postgres columns from JSONB to JSON and lose
  GIN-indexability. The hook renders the shared `JSONColumn` instead.
- **`server_default=sa.func.now()`, not the captured literal.** Autogenerate
  froze SQLite's `CURRENT_TIMESTAMP` into the script; `func.now()` compiles per
  dialect (`now()` on Postgres) so one script stays portable and `alembic check`
  reports no spurious server-default drift. Fixed in the *generator*, not by hand,
  after it recurred on the second migration.
- **The `ruff` post-write hook is registered in Python, not `alembic.ini`.**
  `exec`-type hooks need `ruff` on `PATH` (it lives in `.venv/Scripts/`) and it
  registers no `console_scripts` entry point, so both documented styles raised
  `FileNotFoundError: [WinError 2]` and left migrations unformatted.
  `@register_write_hook("ruff")` shells out to `sys.executable -m ruff`, and
  warns-and-returns on failure so a formatting problem never looks like a
  migration failure.

`render_as_batch=True` is set so SQLite gets create-copy-drop-rename instead of
unsupported `ALTER`s. The DB URL comes from app settings, never `alembic.ini`, so
no credential is ever written to a tracked file; override per-run with
`alembic -x db_url=…`.

### Stage 2 — DONE: auth (local provider + Supabase), FastAPI core

**Verified 2026-08-23. All 39 checks pass.** Re-run any time:

```bash
cd D:\AURA\backend
./.venv/Scripts/python.exe scripts/verify_auth.py     # 39 checks, scratch DB
```

Built: `app/main.py` (app factory + lifespan), `run.py` (dev entry point),
`app/api/router.py`, `app/api/deps.py`, `app/api/routes/{health,meta,auth}.py`,
`app/schemas/auth.py`, `app/auth/{base,local,supabase,tokens,registry}.py`.
21 routes; 12 of them auth. 30th table added: `user_sessions`
(revision `8886a2e17fe8`).

Security model, all of it exercised by the verifier rather than asserted in prose:

- Argon2id passwords, transparent rehash when cost parameters rise.
- Short-lived HS256 access tokens carrying `sid`, re-checked against
  `user_sessions` every request — so **logout invalidates the token on the next
  request**, not at expiry.
- Opaque refresh tokens (48 random bytes, only the SHA-256 hash stored), rotated
  on every use, with replay detection: presenting a rotated token revokes the
  whole `family_id`, newest token included.
- Login timing equalised against a dummy Argon2 hash; wrong password and unknown
  email return byte-identical envelopes. Revoking a stranger's session returns the
  same 404 as a nonexistent one, so neither is an oracle.
- `alg=none`, wrong-key, expired, wrong-audience, wrong-`typ` tokens all rejected.
- Supabase is verify-only and says so: all six capability flags false, unsupported
  operations raise `auth_operation_unsupported` **with a hint**, and `resolve_user`
  mirrors the account locally. Verified offline with locally minted JWTs, since no
  Supabase project exists yet.
- Password reset returns `delivery="not_configured"` rather than implying an email
  was sent — there is no SMTP transport. Outside production the link is returned
  in the response so the flow is completable; only for an address that exists.

Four more real bugs found by executing this stage (numbering continues from the
foundation's three):

4. **`configure_logging()` crashed on the first log call.**
   `structlog.stdlib.add_logger_name` reads `logger.name`, but
   `WriteLoggerFactory` produces `WriteLogger` objects that have none —
   `AttributeError`, application startup failed, server exited. Every `log.info()`
   in the codebase would have raised. Only surfaced by actually booting uvicorn;
   no import-time check would have caught it. Fixed by switching to
   `structlog.stdlib.LoggerFactory()` with the full `ProcessorFormatter`
   integration. Two related honesty problems in the same function were fixed with
   it: the docstring claimed stdlib loggers were redacted when `basicConfig` did
   no such thing (now one root handler with `foreign_pre_chain`, and uvicorn's own
   handlers cleared since it sets `propagate=False`), and `format_exc_info` ran
   *after* the redactor, so a traceback containing a key would have escaped
   unscrubbed.
5. **Reuse detection logged a warning and changed nothing.** `get_db` rolls the
   request session back on any exception, and `refresh()` revokes the token family
   then *raises* — by design, the token is rejected. So the rollback discarded the
   revocation: the stolen-token defence was decorative. All three
   revoke-then-raise paths in `refresh()` now commit in an independent transaction
   (`_revoke_family_durably` / `_revoke_one_durably`). A security control whose
   durability depends on the request succeeding is not a control.
6. **`DateTime(timezone=True)` is a no-op on SQLite.** pysqlite has no timestamp
   type, so the offset is dropped on write and values come back **naive**;
   Postgres `TIMESTAMPTZ` returns aware ones. `UserSession.is_active` therefore
   raised `TypeError: can't compare offset-naive and offset-aware datetimes` on
   the fallback backend while working fine on the target one. Fixed in the type
   system rather than at the comparison: `UTCDateTime` (a `TypeDecorator` in
   `models/base.py`) normalises to UTC on write and re-tags on read, is registered
   in `type_annotation_map` so `Mapped[datetime]` picks it up automatically, and
   replaced all 28 explicit uses. Emits identical DDL, so `alembic check` still
   reports no drift and no migration was needed. This would otherwise have hit the
   scheduler, crawler, and analytics stages the same way.
7. **Registration rejected the verifier's own fixtures — correctly.**
   `email-validator` refuses special-use TLDs (`arpa`, `invalid`, `local`,
   `localhost`, `onion`, `test`), so `@example.test` addresses 422'd. The app was
   right; the fixtures moved to the reserved `.example` TLD, which validates and
   can never be registered. Worth remembering when writing later fixtures.

### Stage 3 — DONE: business API (businesses, profiles, websites)

**Verified 2026-08-26. All 43 checks pass.** Re-run any time:

```bash
cd D:\AURA\backend
./.venv/Scripts/python.exe scripts/verify_business.py   # 43 checks, scratch DB
```

Built: `app/api/routes/businesses.py` (website routes live here too — there is
**no** `routes/websites.py`), `app/schemas/business.py`,
`app/services/business.py`, `app/utils/urls.py`, plus the `app/services/` and
`app/utils/` packages. 31st table: `business_profiles` already existed, so the
migration added only `businesses.onboarding_step` + its CHECK constraint
(revision `719eeee09ef3`, now head). 14 business-scoped method/path pairs.

Design decisions worth preserving:

- **The tenant boundary is a dependency, not a convention.** `OwnedBusiness` →
  `get_path_business` → `get_owned_business` puts `owner_id` in the SQL `WHERE`
  clause, so a route physically cannot load another account's row. Website lookups
  additionally predicate on `business_id`, so one owner's website id is invisible
  from a *different business of the same owner*.
- **404, never 403, for someone else's row.** A 403 on an existing-but-not-yours
  id is an existence oracle. Missing and forbidden return byte-identical `error`
  objects (only the per-request `request_id` differs) — the same choice the auth
  stage made for sessions.
- **`url` vs `normalized_url`.** `url` stays fetchable with query order preserved;
  `normalized_url` sorts the query and makes the root path explicit
  (`https://x` → `https://x/`) and is the dedup key. Two spellings of one address
  collapse to one row, which is what stops a site accumulating parallel scan
  histories. The URL itself is **immutable** — scans, SEO reports, and improvement
  deltas are all measurements *of a specific address*, so editing it in place
  would re-attribute history to a different site.
- **SSRF guard at registration, not just at crawl time.** 14 hostile forms are
  refused through HTTP with `blocked_url`: loopback, `169.254.169.254`, `[::1]`,
  IPv4-mapped IPv6, all RFC 1918 ranges, `metadata.google.internal`, `0.0.0.0`,
  `javascript:`, `file://`, credentials-in-URL, and non-web ports. No partial row
  is written on rejection.
- **Profile completeness is checkable, not asserted (§24).** The response carries
  `missing_fields` alongside `completeness_percent`, so the number can be
  recomputed from the payload — the verifier does exactly that (36% = 4/11) and
  confirms every named field is genuinely empty. A profile is 404 before it is
  created; no empty profile is invented.
- **Exactly one primary website, always.** Promoting demotes the previous primary
  and mirrors onto `businesses.primary_website_url`; deleting the primary promotes
  a replacement; demoting the only website is a 422 `primary_website_required`
  with a hint rather than a silently-broken invariant.

Two more real bugs found by executing this stage (numbering continues from 7):

8. **`min_length=1` on `ShortText` made `_clean_list`'s documented behaviour
   unreachable.** `_clean_list` is specified to trim entries and drop blanks, but
   Pydantic validates **item types before field validators run**, so the item
   constraint fired first. A dynamic list form posting `["Sourdough", ""]` — what
   every "add another row" UI does when a row is left empty — 422'd with
   `services.3: String should have at least 1 character` instead of ignoring the
   blank. Fixed by removing `min_length` from the item type; `max_length` still
   guards the column width. Note this is deliberately the *opposite* choice from
   the scalar fields: a blank *name* is a mistake worth reporting, a blank *row*
   carries no information. Two apparently unrelated failures (a `KeyError` on
   `missing_fields`, and "an earlier onboarding step was erased by a later one")
   were the same bug cascading.
9. **The `.example` TLD is required for email fixtures and forbidden for URL
   fixtures.** Exact mirror image of bug 7: `email-validator` rejects `.test`, so
   emails must use `.example` — but AURA's own SSRF guard blocks the whole
   `.example` TLD, so a website fixture there is refused with `blocked_url` before
   storage. 30 of 43 checks failed on the first run because of this, and **the app
   was right both times**. URL fixtures now use the RFC 2606 *second-level*
   reservations (`example.com`, `example.net`), which are permanently
   unregistrable, parse to a real public suffix, and pass the guard.

Also added this stage, outside the app itself:

- **`backend/pyproject.toml`** — ruff had been running on bare defaults from two
  places (the Alembic post-write hook and editor format-on-save), so lint was not
  reproducible. Now line-length 88, target py313, and a documented select list
  (`E,W,F,I,B,BLE,C4,C90,UP,SIM,RUF,S,DTZ,T20,RET,PTH`). `ARG` is deliberately
  **not** selected: 37 of its 99 findings were on signatures the code does not
  control — the 12 abstract `AuthBackend` methods, SQLAlchemy's
  `process_bind_param(self, value, dialect)`, FastAPI's `lifespan(app)`, Alembic's
  `include_object`. Adding `BLE` retroactively gave meaning to nine pre-existing
  `# noqa: BLE001` directives. State: `ruff check app/ scripts/ alembic/ run.py`
  and `ruff format --check` both clean.
- **`.vscode/launch.json` + `settings.json`** (tracked; `.gitignore` now
  allow-lists exactly these two). Every config pins
  `cwd = ${workspaceFolder}/backend` and the venv interpreter, so F5 runs `run.py`
  from the right directory regardless of which file is open — the
  `ModuleNotFoundError: No module named 'app'` above is now structurally
  unreachable from the editor. `python.analysis.extraPaths` adds `backend/`, or
  Pylance flags every `from app.core…` import as unresolved.

### Stage 4 — DONE: crawler + scan API

**Verified 2026-08-27. All 17 checks pass.** Re-run any time:

```bash
cd D:\AURA\backend
./.venv/Scripts/python.exe scripts/verify_crawler.py   # 17 checks, scratch DB
```

Also demonstrated live end-to-end against a running server (real Argon2, real
HTTP crawl of `example.com`, real DB rows):

```bash
./.venv/Scripts/python.exe -m uvicorn app.main:app        # terminal 1
./.venv/Scripts/python.exe scripts/demo_live.py           # terminal 2
```

Built: `app/crawler/` (6 modules — `fetcher`, `robots`, `sitemap`, `frontier`,
`extract`, `crawler`), `app/services/scan.py`, `app/api/routes/scans.py`,
`app/schemas/scan.py`. **No migration** — the scan tables (`website_scans`,
`website_pages`) were already in the initial schema, so head is still
`719eeee09ef3`. 7 scan routes (an 8th, `GET /{scan_id}/seo`, was added in
Stage 5), all nested under a website so the tenant boundary is inherited.

Design decisions worth preserving:

- **The crawler is AI-free (§36/§37).** Fetch, robots, sitemap, frontier, HTML
  extraction, and aggregation are all genuine application logic. Gemini is not
  imported anywhere in `app/crawler/`.
- **One guarded egress point.** All network I/O goes through `SafeFetcher`, which
  re-checks **every hop** of a redirect chain through `normalize_url` and resolves
  DNS answers through `is_public_ip` — so a 302 to `169.254.169.254` (cloud
  metadata) is refused mid-chain, not just at the seed. The SSRF guard is
  therefore enforced at crawl time, not only at website-registration time.
  `_check_dns` short-circuits (skips resolution) only when `allow_private_hosts`
  is set, which is how the verifier and demo hit `example.com`/localhost fixtures.
- **Scans are async + pollable + immutable.** `POST .../scans` writes a `QUEUED`
  row, commits, dispatches to a thread-pool worker, and returns `202` with the id.
  `execute_scan` is a worker entry point that owns its own commits and writes
  progress live (crawler owns 0–70%: robots 5, sitemap 12, crawl 15→70) so a
  poller sees movement. A second concurrent scan of the same site is `409
  scan_already_running`.
- **Terminal status is honest about what happened.** `_apply_status`:
  robots.txt-disallow → `FAILED`/`crawler_blocked`; zero pages crawled → `FAILED`
  with the homepage's specific fetch cause (`website_timeout`,
  `website_unreachable`); some pages failed → `PARTIAL`; else `COMPLETED`. Every
  count in `site_analysis` re-sums from the persisted per-page rows — the verifier
  reproduces `pages_analysed`, image totals, and sitemap counts from the rows
  themselves (§25, no fabrication).
- **Sitemap probing is bounded.** When robots.txt names no `Sitemap:`, five
  conventional paths are probed and a 404 on each is silent; a declared sitemap is
  fetched instead of probing. `MAX_SITEMAP_DOCUMENTS=10`, `MAX_SITEMAP_DEPTH=2`.

One verifier-fixture lesson (numbering continues from 9; the crawler code itself
had no new bugs this stage):

10. **respx defaults `assert_all_mocked=True`, so an *unmocked* request raises a
    non-httpx error that escapes the fetcher's httpx-only catch.** Three
    negative-path checks (timeout / 5xx / partial) set robots.txt = 404, which
    makes the crawler probe all five `DEFAULT_SITEMAP_PATHS`; only `/sitemap.xml`
    was mocked, so the other four raised inside the crawl and tripped
    `execute_scan`'s generic `except Exception` → the wrong `scan_failed` code,
    masking the specific homepage-failure code under test. Fixed in the fixtures
    (mock all five default paths as 404), not the app — the crawler and
    `_homepage_failure` mapping were correct. Also retired a stale tripwire in
    `verify_business.py` that hard-asserted "14 business routes" and mis-counted
    the 7 new nested `/scans` routes; it now excludes the `/scans` sub-tree, which
    the crawler suite's own auth-wall check owns.

### Stage 5 — DONE: SEO scoring engine

**Verified 2026-08-27. All 19 checks pass; the full suite is now 144 checks
across six files (~45 s).** Re-run any time:

```bash
cd D:\AURA\backend
./.venv/Scripts/python.exe scripts/verify_seo.py   # 19 checks, scratch DB
```

Built: `app/engines/seo/` (`rules.py`, `engine.py`, `__init__.py` — the first
tenant of the `app/engines/` package), `app/services/seo.py`,
`app/schemas/seo.py`, and an 8th scan route `GET .../scans/{scan_id}/seo`. **No
migration** — the report tables (`seo_reports`, `seo_scores`, `seo_issues`) were
already in the initial schema, so head is still `719eeee09ef3`.

Design decisions worth preserving:

- **The engine is pure arithmetic, AI-free (§36).** `score_pages` holds no DB
  session and imports no LLM client; the verifier greps all three engine files for
  `genai`/`gemini`/`openai`/`anthropic` and asserts none appear. Gemini's turn
  comes later (explanation, rewriting), never scoring.
- **Every number reconstructs from its evidence (§24).** 27 rules across 8
  weighted categories (METADATA .18, CONTENT .16, TECHNICAL .14, STRUCTURE .14,
  LINKS .12, SCHEMA .10, IMAGES .08, READABILITY .08 — sum 1.0). Each rule returns
  a fraction in [0, 1]; a page-scoped rule contributes `weight × mean(fraction)`
  over the pages it *applies to*, a site-scoped rule fires once. The reconciliation
  identities the verifier checks against live rows: within a category
  `sum(issue.points_deducted) == points_available − points_earned`;
  `normalized_score == round(points_earned/points_available × 100, 1)`;
  `overall == round(Σ(category.normalized × category.weight), 1)`.
  `points_available` is the sum of the *applicable* rule weights, so a rule that
  does not apply neither earns nor costs.
- **"Not applicable" is not "zero".** A category no rule applied to (IMAGES on a
  site with no images, READABILITY with no readability score) scores 100 against
  its nominal maximum with every check flagged `applicable:false`, rather than
  dividing by zero or silently re-weighting the other categories. The verifier
  asserts exactly this, and that no issue is emitted for such a category.
- **No score is invented for an unscorable crawl (§25).** `score_pages` filters to
  `was_fetched` pages and raises `ValueError` if none remain; the worker catches it
  and stores *no* report. `GET /seo` is then a 404 with a poll hint, never a
  fabricated 0 or 100. A wholly-broken site scores a real 0.0 (21 issues), a clean
  page a real 100.0 (0 issues), a middling http site with no OG/schema a real
  ~64.7 — each reproducible from its issue list.
- **The score is a heuristic, labelled as such (§7/§45).** `engine_version` is
  stamped on every report and the deterministic `summary` calls it an "Estimated
  SEO score", never a ranking guarantee. Scoring the same pages twice is
  byte-identical (deterministic order: homepage first, then depth, then URL), which
  is what lets the improvement loop diff two scans later.
- **Wired into the scan pipeline.** `execute_scan` runs crawl (0–70%) → `SCORING`
  at 70 → `score_and_store` → terminal status at 100 with the score in the message.
  Scoring is idempotent (drops any prior report for the scan first), so re-scoring
  an old scan replaces rather than collides. A scoring error restores the terminal
  *crawl* status with no report rather than failing the whole scan.
- **Issues lead with the biggest wins.** `GET /seo` returns issues sorted
  most-severe-first, then by points lost; the verifier confirms the order and that
  every `rule_id` on the wire exists in the ruleset. Tenant isolation and the auth
  wall are inherited from the nested route (401 unauthenticated, 404 for a
  stranger, never a 403 oracle).

One verifier-fixture lesson (numbering continues from 10; the engine code itself
had no new bugs this stage):

11. **The 8th scan route broke `verify_crawler`'s hard-coded route count.**
    `verify_crawler.py` check 13 asserted exactly "7 scan routes"; adding
    `GET /{scan_id}/seo` made 8, so the crawler suite would have failed the moment
    Stage 5 landed. Bumped the count to 8 — its auth-wall loop already exercises the
    new route (it filters on `/scans` in the path), so the SEO route is checked for
    401 there for free. Same class of stale cross-suite tripwire as the "14 business
    routes" assert retired in Stage 4: a hard count in one suite is a liability the
    next stage pays. (Also folded in a C408 lint fix in the new verifier — `dict(…)`
    fixtures rewritten as literals — since `scripts/` is linted.)

### Stage 6 — DONE: AI-visibility engine

**Verified 2026-08-27. All 20 checks pass; the full suite is now 164 checks
across seven files (~50 s).** Re-run any time:

```bash
cd D:\AURA\backend
./.venv/Scripts/python.exe scripts/verify_ai_visibility.py   # 20 checks, scratch DB
```

Built: `app/engines/ai_visibility/` (`signals.py`, `engine.py`, `__init__.py` —
the second tenant of `app/engines/`), `app/services/ai_visibility.py`,
`app/schemas/ai_visibility.py`, and a 9th scan route
`GET .../scans/{scan_id}/ai-visibility`. **No migration** — the report tables
(`ai_visibility_reports`, `ai_visibility_scores`) were already in the initial
schema, so head is still `719eeee09ef3`.

Design decisions worth preserving:

- **Signals, not issues — the mirror image of SEO.** AI-visibility is a set of
  *positive* signals a page either carries or not (an FAQ block, JSON-LD, a
  byline, a date), never a list of mistakes to deduct for. So there is **no issues
  table**: a category score is simply the share of its available signal weight the
  pages earned. 23 signals across 8 weighted categories (ANSWERABILITY .18,
  SCHEMA .16, STRUCTURE .14, ENTITY_CLARITY .12, COMPLETENESS .12, TRUST .10,
  CITATION_READINESS .10, FRESHNESS .08 — sum 1.0).
- **Same arithmetic as SEO, so it reconciles identically (§24).** Per signal
  `points == round(weight × fraction, 4)`; per category
  `earned == round(Σ signal points, 4)` and
  `normalized == round(earned/available × 100, 1)`; overall
  `== round(Σ(normalized × weight), 1)`. The verifier reconstructs all three from
  the stored `signal_results` on live rows. Every category is guaranteed
  `points_available > 0` (the `positive_ai_points_available` CHECK), which holds
  because each category has at least one always-applicable signal.
- **The engine is pure and AI-free (§36/§37).** No DB session, no wall-clock read
  (the reference instant arrives as `as_of`), and — the point of the stage — no
  LLM client. The verifier proves this by **parsing each engine file's AST** and
  inspecting only its `import`/`from` nodes against a forbidden set (genai, gemini,
  generativeai, openai, anthropic), *not* by grepping text — the module docstrings
  legitimately name ChatGPT/Gemini/Perplexity as the assistants the heuristic is
  about, and that honest framing must not trip the check.
- **It is a heuristic, and says so on every row (§7/§45).** The
  `HEURISTIC_DISCLAIMER` ("… does not measure or guarantee how any AI system ranks,
  cites, or recommends your site") is stored per report, the deterministic summary
  calls it "Estimated AI-search readiness", and `engine_version` is stamped so the
  improvement loop refuses to compare across signal-set changes.
- **"Not applicable" is not "zero".** A page signal that applies to no page (an
  FAQ-depth check where nothing has an FAQ) is recorded `applicable:false` with
  `points == max == 0.0` and excluded from the totals — a non-event, not a failure.
  The verifier asserts this for `faq_depth`, `in_depth_content`, and
  `recent_updates` on a site that lacks each.
- **Runs beside SEO, independently.** `execute_scan` now scores *both* engines,
  each in its own transaction (`_score_with`): a bug in one can neither lose the
  other's report nor lose the crawl, and whatever succeeds is durable. The terminal
  progress message carries each headline that was produced
  (`… · SEO 65/100 (fair) · AI 29/100 (critical)`).

One verifier-methodology lesson (numbering continues from 11; the engine code
itself had no new bugs this stage):

12. **Grepping file *text* for "gemini" is the wrong test for "imports no LLM".**
    The first `_no_llm` check scanned for substrings and tripped on the signals
    docstring, which names ChatGPT/Gemini/Perplexity as the assistants the score is
    *about* — a false positive on correct, honest prose. Rewritten to parse the AST
    and inspect only `import`/`from` statements, which is precisely the claim ("this
    module imports no generation client"). The test now matches the property, and
    the prose naming the assistants — the honest framing the disclaimer depends on —
    is free to stay.

### Stage 7 — recommendation engine (DONE)

**Verified 2026-08-27. All 14 checks pass; the full suite is now 178 checks
across eight files.** `app/engines/recommendation/` deterministically ranks SEO
issues and applicable missing/partial AI-visibility signals using score-derived
impact and an explicit remediation-effort catalog. The stored action list is
tenant-scoped, traceable to its `rule_id`/`signal_id`, and reconciles across
scans: observed absences complete open work, recurrences reopen it, and manual
statuses/dismissals remain user-owned. Migration `4f51c3d5e28a` enforces the
documented `(business_id, rule_id)` dedup key at the database layer. The API
provides list/filter/detail/status/regenerate endpoints. *(verify_recommendation,
14)*

### Next up (spec §53 order)

**→ Stage 8: Gemini service layer** — add guarded, structured generation for
explanations and rewrites only. Recommendation ranking, impact, effort, and
lifecycle remain deterministic application logic; model responses must be
validated as untrusted input. Confirm the current `google-genai` pin works with
the configured `AQ.` key before making it a build dependency.

Then content pipeline → publishing connectors → scheduler →
analytics → competitor analysis → AI assistant → improvement engine →
notifications/activity/settings → frontend → landing page → tests → docs →
deployment → final no-fake-functionality audit.

Still missing from scaffolding: `Dockerfile`, `docker-compose.yml`, `README.md`,
and package dirs `connectors/`, `workers/`, `tests/`. (`app/engines/` now holds
`seo/` and `ai_visibility/`; the crawler is its own `app/crawler/` package.)

Noted for the Gemini stage: `google-genai==0.5.0` is a late-2024 pin while the
configured key uses the newer `AQ.` format — needs a live call to confirm before
building on it.

---

## Running the app

```bash
cd backend
python -m uvicorn app.main:app --reload        # dev server (no run.py in this tree)
```

Then: <http://127.0.0.1:8000/docs> · `/health` · `/api/v1/meta/capabilities`.

There is no `run.py` in the current tree (the earlier dev entry point was
retired); the canonical forms are `uvicorn app.main:app --reload` for dev and
`backend/docker-entrypoint.py` (wait-for-Postgres → `alembic upgrade head` →
uvicorn with 2 workers) inside Docker. The project ships no `alembic.ini` on
purpose — the database URL must never live in a tracked file — so migrations
run programmatically via `alembic.command.upgrade` with a bare `Config`
(pointed at `alembic/`), exactly as `docker-entrypoint.py` and the verify
scripts do.

```bash
cd frontend
npm install && npm run dev                    # -> http://localhost:3000
```

Copy `.env.example` to `backend/.env` (and `frontend/.env.local` for the
frontend) before running.

## Verification scripts

Every stage leaves an executable proof behind. Run everything with the single
documented command (12 `verify_*.py` suites, ~178 checks, plus `pytest -q`):

```bash
cd backend
python scripts/run_all_checks.py
```

It exits 0 only if every suite and pytest pass, and prints a summary table
(`--dry-run` lists the order without executing). Individual suites still run
directly, e.g. `python scripts/verify_foundation.py --scratch` (18 checks),
`python scripts/verify_auth.py` (39), `python scripts/verify_seo.py` (19),
`python scripts/verify_ai_visibility.py` (20), `python scripts/verify_recommendation.py`
(14) — plus verify_migrations, verify_business, verify_crawler, verify_analytics,
verify_assistant, verify_improvement, and verify_system. `scripts/demo_live.py`
is excluded from the one command: it drives a *live* server, not a scratch DB.

They are not unit tests — they execute the real thing (real migrations, real
ASGI app, real Argon2, real JWTs, the real crawler against mocked HTTP, the real
SEO and AI-visibility scorers) and assert the *honesty* claims specifically: that
a forged `mode="live"` row is rejected by the database, that the configured Gemini
key cannot appear in a log line, that no credential survives in the sessions table,
that demo data is labelled demo, that one tenant cannot read another's, that a
redirect to the cloud-metadata IP is refused mid-chain, that every scan aggregate
is reproducible from the per-page rows the API returns, that an SEO score
reconstructs exactly from the per-issue points that produced it, that an
AI-visibility score reconstructs the same way from its per-signal points, and that
neither scoring engine imports an LLM client (proven by AST-inspecting each
module's imports, not by grepping its prose).

For a narrated live walkthrough against a running server (register → business →
website → a real crawl of `example.com` → analysed pages), see
`scripts/demo_live.py`.


---

## Conventions

- Build depth-first: each stage left runnable and verified before the next.
- Postgres is the target, but keep everything working on the SQLite fallback too.
- Put secrets only in `backend/.env` (git-ignored). `.env.example` documents each.
