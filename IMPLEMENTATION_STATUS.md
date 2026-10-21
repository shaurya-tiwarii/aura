# AURA — Implementation Status

Updated: 2026-09-25 (post-rework snapshot)

## Product state

AURA is an end-to-end, modular-monolith implementation of the Digital
Visibility Optimization Platform. Working backend foundation, functional
Next.js dashboard, website analysis, deterministic scoring, AI-search
readiness analysis, recommendations, business-grounded content
generation/optimization, publishing/scheduling infrastructure, analytics
normalization with provenance, assistant, and continuous monitoring/
improvement workflows.

**Competitor tracking was removed on 2026-09-25 per the user's explicit
approval.** No competitor modules, tables, routes, or UI remain.

## Core flow

```text
Business
  -> Website
  -> Crawl (20-page cap, SSRF-guarded)
  -> SEO + AI-search analysis (DVO-SEO-SCORE-V2.0, 23-signal AI engine)
  -> Recommendations (impact x effort)
  -> Content opportunities
  -> Content generation/optimization (Gemini 2.5 Flash)
  -> Approve ("Mark ready")
  -> Publishing/scheduling (demo/live)
  -> Analytics (provenance-labelled)
  -> Continuous monitoring
  -> Improvement comparison
  -> New recommendations
```

## Implemented

- Tenant-isolated authentication and sessions (Argon2id, rotating refresh
  tokens with replay detection; frontend silent refresh-on-401)
- Business/profile/onboarding management
- SSRF-safe website crawling: same-domain discovery, URL normalization,
  robots.txt + sitemap processing, 20-page cap, depth 3, typed crawl/fetch
  states, pollable scan progress
- SEO engine DVO-SEO-SCORE-V2.0: 27 rules over 8 weighted categories,
  versioned rule IDs, explainable issues, exact score reconciliation
- AI-visibility engine: 23 deterministic heuristic signals over 8 weighted
  categories, per-row disclaimer, LLM-free (AST-inspected)
- Recommendation engine: impact (score-gain arithmetic) x effort (fixed
  table), quick_win/major_project/fill_in/long_term buckets, `(business_id,
  rule_id)` reconciliation and lifecycle
- Content generation/optimization with Gemini 2.5 Flash, Pydantic validation,
  version history, "Mark ready" approve step
- Content-opportunity lifecycle
- Demo + WordPress + LinkedIn + Reddit connector architecture
  (demo honestly labelled until real credentials exist)
- Scheduling through APScheduler: DB as source of truth, persistent
  registration on restart, advisory-lock leadership across workers,
  scheduled-post retries with exponential backoff, publish-failure
  persistence
- Recurring website monitoring (daily/weekly)
- Automatic before/after improvement comparison after scheduled/manual scans
  (only when `engine_version` matches)
- Notifications and activity logging for spine events (scan complete, content
  generated, publish success/failure, scheduled execution, score improvement)
- Analytics normalization, provenance tracking (`platform_api`/`demo`/
  `user_entered`/`derived` labelled in the UI), snapshots
- Tenant-scoped assistant grounded in business evidence
- Responsive Next.js/React dashboard + landing page (hand-rolled CSS; hero
  artwork explicitly de-faked and labelled)
- Rate limiting (slowapi) on auth/scans/content/AI endpoints
- Docker/Compose deployment scaffolding (backend entrypoint, multi-stage
  frontend Dockerfile, Postgres service, health checks)

## Remaining external limitations

Real social publishing and analytics collection still depend on each
platform's official credentials/OAuth permissions. Demo connectors/data
remain available and are clearly labeled. No external platform is represented
as live without valid credentials.

## Validation

- `cd backend && python scripts/run_all_checks.py` — the single documented
  command: runs all 12 `backend/scripts/verify_*.py` suites (~178 checks) and
  the pytest regression suite; exits non-zero on any failure.
- Post-rework state (2026-09-25): all verification suites green; pytest 11
  passed; frontend `tsc` 0 errors, `next build` succeeds.
- Earlier isolated DVO crawler fixtures: 20/20 passed.

## Versioning note

The production engines are **DVO-SEO-SCORE-V2.0** and the 23-signal
AI-visibility engine documented in `METHODOLOGY.md`. These supersede the
frozen 10-check V1 draft — V1 is a historical baseline, never the contract.
Scores must not be compared across ruleset versions without the recorded
engine version. (Rows currently persist `engine_version="1.0"`; the V2 names
should replace these in a follow-up change — see CHANGELOG.)
