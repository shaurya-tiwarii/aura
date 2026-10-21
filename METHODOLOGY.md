# AURA — Scoring & Methodology (viva reference)

> **Versioning rule (frozen):** any change to rule/signal weights, thresholds,
> aggregation, or semantics requires a new methodology version. Never compare
> scores computed under different versions — every persisted report stores its
> `engine_version` precisely to prevent that.

## 1. What this document is

This is the **production scoring contract** for the AURA major project
(Group IB13, "Digital Visibility Optimization Platform"). It documents what the
code in `backend/app/engines/` and `backend/app/crawler/` actually does —
verified against the source on 2026-09-25. Everything here must be defensible
in the viva: every number below can be traced to a constant or function in the
codebase.

## 2. Version authority — V2 supersedes V1

The project history contains an earlier frozen draft,
**DVO-SEO-SCORE-V1.0** (10 checks, 100 points, described in the master context
document). That draft is **superseded and no longer the contract**:

| Aspect | DVO-SEO-SCORE-V1.0 (archived draft) | **DVO-SEO-SCORE-V2.0** (production) |
|---|---|---|
| Checks | 10 | **27 rules** |
| Categories | 10 flat checks | **8 weighted categories** |
| Aggregation | 10–100 points | Deduction-from-perfect with category weights summing to **1.0** |
| Traceability | fixed rubric | Every deduction carries its `rule_id`; the headline score reconstructs exactly from rule-level parts |

**Never present V1 as the contract.** Where V1 appears in older context
documents, it is a methodology baseline only.

**Version naming (done 2026-09-25):** the code records these exact strings —
`ENGINE_VERSION = "DVO-SEO-SCORE-V2.0"` for the SEO engine
(`backend/app/engines/seo/rules.py`) and `ENGINE_VERSION = "DVO-AI-VIS-V2.0"`
for the AI-visibility engine (`backend/app/engines/ai_visibility/signals.py`) —
and stores them on report rows, so stored rows carry the same version language
as this document. Because version strings changed, scans produced before this
rename are correctly treated as incomparable (`is_comparable=false`) rather
than silently compared.

## 3. SEO engine — DVO-SEO-SCORE-V2.0

Deterministic, LLM-free (`backend/app/engines/seo/`). Given the pages a scan
captured plus whole-site facts (robots.txt, sitemap presence, business
keywords), it returns a 0–100 score.

### 3.1 The 27 rules, 8 categories

Weights sum to 1.0 and are validated at import (the module raises if the total
is not 1.0 or a category is uncovered):

| Category | Weight | Rules (`rule_id`) |
|---|---:|---|
| METADATA | 0.18 | `metadata.title_missing`, `metadata.title_length`, `metadata.description_missing`, `metadata.description_length`, `metadata.viewport_missing`, `metadata.open_graph_missing` |
| CONTENT | 0.16 | `content.thin_content`, `content.low_word_count`, `content.no_target_keywords` |
| TECHNICAL | 0.14 | `technical.not_https`, `technical.no_robots_txt`, `technical.no_sitemap`, `technical.noindex`, `technical.canonical_missing`, `technical.lang_missing`, `technical.slow_response` |
| STRUCTURE | 0.14 | `structure.h1_missing`, `structure.multiple_h1`, `structure.heading_hierarchy`, `structure.no_subheadings` |
| LINKS | 0.12 | `links.broken_links`, `links.no_internal_links` |
| SCHEMA | 0.10 | `schema.no_structured_data`, `schema.no_organization` |
| IMAGES | 0.08 | `images.missing_alt` |
| READABILITY | 0.08 | `readability.hard_to_read`, `readability.grade_too_high` |

Each rule has a point weight; a category's available points equal the sum of
its rules' weights.

### 3.2 Aggregation (the reconciliation guarantee)

1. **Page scope:** each rule evaluates either per page (`evaluate`) or per
   site (`evaluate_site`). Failed fetches are excluded from scoring entirely.
2. **Category score:** `earned = available − Σ points_deducted`, then
   `normalized = round(earned / available × 100, 1)`. **A rule that does not
   apply to any page scores full marks ("not applicable" = 100), not a
   penalty**, and does not re-weight the other categories.
3. **Headline:** `overall = round(Σ category.normalized × category.weight, 1)`.
4. **Grade bands:** excellent ≥ 90, good ≥ 75, fair ≥ 60, poor ≥ 40,
   critical < 40.
5. **Empty crawl:** scoring raises `ValueError` when there is nothing
   analysable — the API returns no report rather than inventing a score.

The executable proof (`backend/scripts/verify_seo.py`) asserts the identities
above hold on real stored reports, so every deduction is traceable to its
`rule_id` and no number appears that the evidence cannot reproduce.

### 3.3 What grounds the rules

The rules encode publicly documented SEO practice applied to *observed* HTML:
metadata presence/length, content volume, HTTPS, robots/sitemap availability,
heading hierarchy, internal/external links (broken links are genuinely probed —
see §6), structured data, image alt coverage, and readability. Thresholds are
documented in the code and in the per-issue `suggested_fix`/`describe` text.
The engine makes **no claims about ranking** — it measures observable on-page
factors only.

## 4. AI-visibility engine — estimated AI-search readiness

Deterministic, LLM-free (`backend/app/engines/ai_visibility/`). It scores a
different question from SEO: *how well is this site's content structured for an
AI system that needs to extract, quote, and attribute an answer?*

### 4.1 The 23 signals, 8 categories

Signals are **positive facts (present/absent)** — the mirror image of SEO's
issues table. Same weighting validation at import (weights sum to 1.0):

| Category | Weight | Signals (`signal_id`) |
|---|---:|---|
| ANSWERABILITY | 0.18 | `answerability.faq_present`, `answerability.question_headings`, `answerability.faq_depth` |
| SCHEMA | 0.16 | `schema.structured_data_present`, `schema.organization_entity`, `schema.answer_rich_types` |
| STRUCTURE | 0.14 | `structure.clear_headings`, `structure.scannable_blocks`, `structure.single_main_heading` |
| ENTITY_CLARITY | 0.12 | `entity_clarity.descriptive_titles`, `entity_clarity.meta_descriptions`, `entity_clarity.language_declared` |
| COMPLETENESS | 0.12 | `completeness.substantial_content`, `completeness.in_depth_content`, `completeness.topic_breadth` |
| TRUST | 0.10 | `trust.contact_info`, `trust.secure_connection` |
| CITATION_READINESS | 0.10 | `citation_readiness.authorship`, `citation_readiness.indexable`, `citation_readiness.canonical_url`, `citation_readiness.outbound_citations` |
| FRESHNESS | 0.08 | `freshness.dates_present`, `freshness.recent_updates` |

Aggregation mirrors the SEO engine (category normalized scores weighted-summed
to a 0–100 headline), verified on live rows by
`backend/scripts/verify_ai_visibility.py`.

### 4.2 The heuristic contract (non-negotiable for the viva)

- **This score is a heuristic over observable content and structural signals.
  It does not and cannot measure how any AI system (ChatGPT, Gemini,
  Perplexity, Google, …) ranks, cites, or recommends a site.**
- That statement is stored as `disclaimer` **on every report row** so the
  caveat travels with the data into the API and the UI, never living only in
  frontend copy that could drift:

  > "The AI Visibility Score is an estimate of AI-search readiness, calculated
  > from content and structural signals on your website. It is a heuristic: it
  > does not measure or guarantee how any AI system ranks, cites, or recommends
  > your site."

- The UI label must remain "Estimated AI-search readiness" (or equivalent);
  never a ranking guarantee. The verifier proves by AST-inspection that no
  language model is imported in this engine — the heuristic is genuine
  application logic.

## 5. Recommendation engine — impact × effort priority

Deterministic (`backend/app/engines/recommendation/`). It folds stored SEO
issues and AI-signal gaps into ranked, lifecycle-managed recommendations.

### 5.1 Impact (derived from the score arithmetic)

Impact is **not** a human rating: it is the estimated headline-point gain the
fix would recover, computed as
`gain = points_lost / points_available × 100 × category_weight`. Thresholds
(defined once in `catalog.py`):

- **high** impact: gain ≥ 4.0 headline points
- **medium** impact: gain ≥ 1.5
- **low** impact: below 1.5

### 5.2 Effort (human judgement, fixed table)

`EFFORT_BY_RULE` / `EFFORT_BY_SIGNAL` in `catalog.py` assign one of
`trivial` (minutes, weight 1.0), `low` (< 1 hour, 1.5), `medium` (a few hours,
3.0), `high` (multi-day or ongoing, 5.0) to every rule/signal. A missing entry
is a **startup error**, not a silently mis-ranked list.

### 5.3 Ranking and buckets

- `priority_score = impact.weight / effort.weight` (impact weights 3/2/1) —
  higher sorts first: the biggest gain for the least work wins.
- `priority_bucket` groups for the UI (medium impact counts as "high" for
  bucketing):
  - **quick_win** — high/medium impact, trivial/low effort
  - **major_project** — high/medium impact, medium/high effort
  - **fill_in** — low impact, trivial/low effort
  - **long_term** — low impact, medium/high effort
- Recommendations reconcile across scans on the `(business_id, rule_id)` key,
  and the improvement loop credits a recommendation only when its specific
  `rule_id` issue disappears in a later scan — verified by
  `backend/scripts/verify_improvement.py`.

## 6. Crawler contract

`backend/app/crawler/` (fetch, robots, sitemap, frontier, extract). The crawler
owns discovery, fetching, and metadata; the engines consume its result and
know nothing about HTTP.

- **Budgets:** hard cap **20 pages** per scan (`CRAWLER_MAX_PAGES`, enforced
  with a `ge=1, le=20` validator so it cannot be raised without a code change),
  max depth **3** (`CRAWLER_MAX_DEPTH`), 5 MiB response cap, 15 s per-request
  timeout, 0.5 s politeness delay, 4-way breadth-first fetch waves
  (deterministic order).
- **Politeness:** `robots.txt` is read **before** any page request (and yields
  `Sitemap:` directives and `Crawl-delay`); sitemaps seed the frontier.
- **SSRF protection:** all egress goes through `SafeFetcher`, which resolves
  hosts, refuses private/loopback addresses, and re-checks every redirect hop
  (including DNS-rebinding protection). `CRAWLER_ALLOW_PRIVATE_HOSTS` must stay
  `false` outside tests.
- **Broken links are real:** the `links.broken_links` rule probes up to
  **50 distinct external URLs** per scan (`CRAWLER_MAX_LINK_CHECKS`; 0
  disables), via HEAD with a bodyless-GET fallback, under the same SSRF
  guards.
- Nothing raises on a site problem: 500s, timeouts, and disallowed paths are
  recorded as findings, never as crashes.

## 7. What grounds the rubrics (defense summary)

1. **Traceability:** every deduction points at a `rule_id`/`signal_id`; the
   headline reconstructs exactly from its parts (executable proofs, not just
   claims).
2. **Honesty boundaries:** "not applicable" never penalizes; an unscorable
   crawl produces no report; the AI-visibility disclaimer lives on the data
   row.
3. **Version discipline:** methodology changes require a new version; scores
   are only comparable when `engine_version` matches.
4. **LLM separation:** scoring, crawling, and recommendation ranking are
   LLM-free by construction and by AST-inspected test; Gemini is confined to
   generation, rewriting, explanation, summarization, and chat.
