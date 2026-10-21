# AURA Backend Changelog

## 2026-09-25 — Backend rework

### Removed (dead dependencies)

Removed from `requirements.txt` after `grep` confirmed zero imports anywhere
under `app/`, `alembic/`, or `scripts/`:

- `python-dateutil` — no date parsing outside the stdlib + SQLAlchemy.
- `tenacity` — no retry decorators in use; scheduled-post retries are
  implemented explicitly in `app/scheduler/service.py` with exponential
  backoff so the budget and next-attempt time are inspectable rows.
- `markdown` — no Markdown rendering path.
- `bleach` — no HTML sanitisation path.
- `freezegun` — test-only helper with no test imports.

No product subsystem (crawler, SEO/AI-visibility engines, auth, publishing,
recommendations, scheduler) was removed; only unused third-party packages.

### Changed

- **Scheduler leadership** (`app/scheduler/service.py`): with uvicorn running
  multiple workers, only one process may own APScheduler. Leadership is a
  PostgreSQL advisory lock (`pg_try_advisory_lock`, fixed key `2026092501`)
  held on a dedicated `NullPool` connection for the process lifetime; only
  the lock holder calls `scheduler.start()`. SQLite assumes a single local
  process. Standby workers serve API traffic only. If the leader dies, the OS
  closes its connection, Postgres releases the lock, and the respawned worker
  re-runs the lifespan and wins the now-free lock. Startup runs
  `register_pending()` and the leader adds a five-minute reconciliation job
  that re-registers due scheduled rows (rows created through standby API
  workers) and removes stale jobs.
- **Scheduled publishing retries**: a failed scheduled attempt increments
  `retry_count` and, while budget remains, keeps the row `SCHEDULED` with
  `next_retry_at = now + base_backoff * 2 ** (retry_count - 1)`
  (`SCHEDULER_RETRY_BACKOFF_SECONDS`, default 300); the APScheduler job is
  re-armed for that time. Exhaustion marks the row `FAILED`. Success marks
  it `PUBLISHED`. Each retry and the final outcome emit notifications and
  activity entries.
- **Publish-failure persistence** (`app/services/publishing.py`,
  `app/api/routes/publishing.py`): `_publish_post` marks the `Post` failed
  before raising; `PublishFailedError` carries the `post_id`; the
  `POST /posts/publish` route commits the failed row and returns it with
  HTTP 200 instead of rolling the attempt away.
- **Rate limiting** (`app/core/rate_limit.py`, `app/main.py`): SlowAPI is
  attached, not removed. Auth mutations default to 20/min
  (`RATE_LIMIT_AUTH_PER_MINUTE`), scan creation and content/AI routes have
  per-hour budgets (`RATE_LIMIT_ANALYSIS_PER_HOUR`,
  `RATE_LIMIT_AI_PER_HOUR`), everything else falls back to
  `RATE_LIMIT_DEFAULT_PER_MINUTE`. 429s use the standard AURA error
  envelope with code `rate_limited`.
- **Broken-links SEO rule** (`app/crawler/fetcher.py`,
  `app/crawler/crawler.py`): the `links.broken_links` rule is now real.
  After page crawling, at most `CRAWLER_MAX_LINK_CHECKS` (default 50)
  distinct external URLs are verified with HEAD (bodyless GET fallback for
  405/501), no bodies downloaded, under the same SSRF guards as the crawl;
  results are attributed per page to `PageExtraction.broken_link_count`.
  Links the guard refuses are reported as unchecked, never as broken.
- **Page limit**: canonical production cap is 20 pages
  (`CRAWLER_MAX_PAGES=20`); stale "25" comments corrected.
- **Config**: `SUPABASE_PUBLISHABLE_KEY` accepted as a deprecated alias for
  `SUPABASE_ANON_KEY`; canonical scheme is `SUPABASE_URL` /
  `SUPABASE_ANON_KEY` / `SUPABASE_JWT_SECRET`. `GEMINI_MODEL` defaults to
  `gemini-2.5-flash` (`.env.example` corrected).
- **Product-spine events**: content generation, publish success/failure, and
  scheduled retry/failure now emit notifications + activity entries alongside
  the existing scan-completion and score-improvement events.
- **Migrations** (`alembic/versions/..._add_website_monitoring.py`): rewritten
  to use `op.batch_alter_table` — the previous version called
  `op.create_unique_constraint` / `op.create_check_constraint` /
  `op.alter_column` outside batch mode and could never run on SQLite.
  Revision id unchanged; end state on Postgres identical.
- **Container startup** (`backend/docker-entrypoint.py`, `docker-compose.yml`):
  the entrypoint waits for Postgres TCP, runs `alembic upgrade head`
  programmatically (no `alembic.ini` is shipped on purpose), then execs
  uvicorn with 2 workers. `backend/.env` is optional in Compose.

### Not verified

- Docker image build / Compose startup: no Docker runtime in this environment.
