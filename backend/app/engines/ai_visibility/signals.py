"""The AI-visibility signal set: every measurable AI-search-readiness check.

Companion to :mod:`app.engines.seo.rules`, same declarative shape, different
question. Where the SEO ruleset asks "will a search engine rank this?", this asks
"can an AI system *extract, quote, and attribute* an answer from this?" -- the
signals an assistant like ChatGPT, Gemini, or Perplexity leans on when it decides
whether a page is a usable source.

Honesty, front and centre (spec sections 7 and 45)
--------------------------------------------------
This is a **heuristic over observable content and structure**. Every signal below
is a real thing the crawler measured on the page -- an FAQ section, a JSON-LD
block, a published date, a byline -- never a guess about how any model actually
behaves. The engine cannot and does not measure ChatGPT/Gemini/Perplexity ranking
or citation; it estimates *readiness*. That caveat is stored on every report row
(``AiVisibilityReport.disclaimer``), so it travels with the number.

Scoring model, identical arithmetic to the SEO engine so the numbers reconcile
(:mod:`app.engines.ai_visibility.engine` does the sums):

* A **page-scoped** signal is evaluated on every page it *applies* to and returns
  a fraction in ``[0, 1]`` -- today every page signal is binary (0 or 1 per page),
  so the signal's fraction is simply the share of applicable pages that have it.
  Its contribution is ``weight x mean(fraction across applicable pages)``. A page
  the signal does not apply to (an FAQ-depth check on a page with no FAQ) is
  excluded from that mean and from the category's available points.
* A **site-scoped** signal is evaluated once against the whole-site context and
  returns ``0.0`` or ``1.0``.

A category's available points are the sum of its applicable signal weights; its
earned points are the sum of each signal's ``weight x fraction``. So the per-signal
record (:data:`AiVisibilityCategoryScore.signal_results`) always reconstructs the
category score, and the weighted category scores reconstruct the headline -- the
same explainability guarantee SEO makes, without an issues table (there are no
"deductions" here, only signals present or absent).

Deliberately, no field is scored in two categories at once: FAQ/question shape is
answerability, list/table/heading shape is structure, JSON-LD is schema, and so
on. Reusing the same underlying page field (e.g. the heading outline) to measure a
*different property* in a different category -- "are headings phrased as
questions?" vs "is the heading hierarchy valid?" -- is intentional and not
double-counting: the two are independent facts about the page.
"""

from __future__ import annotations

import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from app.models.enums import AiVisibilityCategory

if TYPE_CHECKING:
    from app.models.website import WebsitePage

# The signal-set version. Baked into every report so the improvement loop refuses
# to compare two AI-visibility scores produced by different signals or weights
# (which would otherwise look like a real change on the site). Bump on any change
# to a weight, threshold, or the signal set below.
ENGINE_VERSION = "DVO-AI-VIS-V2.0"

# Content older than this (by its newest published/modified date) no longer counts
# as a freshness signal. Two years is forgiving on purpose: a small-business page
# is not stale the moment it passes a year, and over-penalising rarely-updated but
# still-accurate pages would be dishonest.
_RECENCY_DAYS = 730

# Each category's share of the headline score (spec section 7's AI-visibility
# groups). Answerability and schema lead because answer-shaped content and
# machine-readable markup are what an extractive AI system actually consumes;
# freshness trails because a correct evergreen page is still citable. MUST sum to
# 1.0 (asserted at import), so the weighted average of the per-category 0-100
# scores is itself a 0-100 score.
CATEGORY_WEIGHTS: dict[AiVisibilityCategory, float] = {
    AiVisibilityCategory.ANSWERABILITY: 0.18,
    AiVisibilityCategory.SCHEMA: 0.16,
    AiVisibilityCategory.STRUCTURE: 0.14,
    AiVisibilityCategory.ENTITY_CLARITY: 0.12,
    AiVisibilityCategory.COMPLETENESS: 0.12,
    AiVisibilityCategory.TRUST: 0.10,
    AiVisibilityCategory.CITATION_READINESS: 0.10,
    AiVisibilityCategory.FRESHNESS: 0.08,
}

# schema.org types that identify the business entity, lower-cased for matching.
_ORG_SCHEMA_TYPES = frozenset({"organization", "localbusiness", "corporation", "store"})
# schema.org types an extractive AI system can lift a structured answer from.
_RICH_SCHEMA_TYPES = frozenset(
    {
        "faqpage",
        "qapage",
        "howto",
        "article",
        "blogposting",
        "newsarticle",
        "product",
        "recipe",
        "event",
        "breadcrumblist",
    }
)

# Leading words that make a heading a question even without a trailing "?".
_INTERROGATIVES = frozenset(
    {
        "how",
        "what",
        "why",
        "when",
        "where",
        "who",
        "whom",
        "whose",
        "which",
        "can",
        "could",
        "do",
        "does",
        "did",
        "is",
        "are",
        "was",
        "were",
        "should",
        "will",
        "would",
        "may",
        "might",
    }
)


@dataclass(frozen=True)
class AiSiteContext:
    """Whole-site facts a signal needs that are not on a single page.

    Assembled once by the engine from the analysable pages, so a site-scoped
    signal (does the site have Organization schema? contact details anywhere?)
    and the page-scoped signals read from the same snapshot. ``as_of`` is the
    reference instant freshness is measured against -- the scan's completion time
    in production, a fixed value in the verifier -- which keeps the engine a pure
    function of its inputs rather than of the wall clock.
    """

    is_https: bool
    analysable_page_count: int
    as_of: datetime
    # Lower-cased union of every schema.org type seen anywhere on the site.
    site_schema_types: frozenset[str]
    has_faq_anywhere: bool
    has_contact_info_anywhere: bool


# A signal evaluator returns (fraction_in_0_to_1, evidence_dict). For a page
# signal the evidence is per-page and the engine aggregates it; for a site signal
# the evidence is the site fact itself and is stored verbatim.
PageEval = Callable[["WebsitePage", AiSiteContext], "tuple[float, dict]"]
SiteEval = Callable[[AiSiteContext], "tuple[float, dict]"]
PageApplies = Callable[["WebsitePage", AiSiteContext], bool]
Describe = Callable[[dict], str]


@dataclass(frozen=True)
class Signal:
    """One AI-visibility check.

    ``scope`` selects which evaluator the engine calls. A ``page`` signal provides
    ``applies`` (default: every analysable page) and ``evaluate``; a ``site``
    signal provides ``evaluate_site``. ``describe`` turns an evidence dict into the
    human-readable sentence stored per signal, so the score is explainable down to
    each individual check (spec section 7). ``title`` is the short noun phrase used
    to roll signals up into the category explanation.
    """

    signal_id: str
    category: AiVisibilityCategory
    scope: Literal["page", "site"]
    weight: float
    title: str
    describe: Describe
    applies: PageApplies | None = None
    evaluate: PageEval | None = None
    evaluate_site: SiteEval | None = None
    _tags: tuple[str, ...] = field(default=(), compare=False)


def _text(value: str | None) -> str:
    """A stripped string, treating None/whitespace-only as empty."""
    return (value or "").strip()


def _as_utc(dt: datetime) -> datetime:
    """Tag a naive datetime as UTC; leave an aware one alone.

    The DB returns aware datetimes (``UTCDateTime``), and production passes an
    aware ``as_of`` -- so real comparisons are aware-vs-aware. This only guards the
    degenerate case of a naive value slipping through, rather than raising a
    ``TypeError`` mid-score.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _newest_date(page: WebsitePage) -> datetime | None:
    """The most recent of a page's published/modified dates, or None."""
    dates = [d for d in (page.published_date, page.modified_date) if d is not None]
    if not dates:
        return None
    return max(_as_utc(d) for d in dates)


def _has_question_heading(page: WebsitePage) -> bool:
    """Whether any heading on the page is phrased as a question.

    A heading counts if it ends in ``?`` or opens with an interrogative
    (how/what/why/...). This is the shape an assistant matches a user's question
    against, so it is the clearest page-level answerability signal we can read from
    the extracted heading outline.
    """
    for heading in page.heading_outline or []:
        text = _text(str(heading.get("text", "")))
        if not text:
            continue
        if text.endswith("?"):
            return True
        parts = text.lower().split(maxsplit=1)
        first = parts[0].strip(string.punctuation) if parts else ""
        if first in _INTERROGATIVES:
            return True
    return False


# ============================================================================
# ANSWERABILITY -- is the content shaped as answers an AI can lift? (weight 0.18)
# ============================================================================
_ANSWERABILITY_SIGNALS: list[Signal] = [
    Signal(
        signal_id="answerability.faq_present",
        category=AiVisibilityCategory.ANSWERABILITY,
        scope="site",
        weight=3.0,
        title="FAQ or Q&A content",
        describe=lambda e: (
            "The site has an FAQ or Q&A section, which AI assistants can lift "
            "question-and-answer pairs from directly."
            if e["found"]
            else "The site has no FAQ or Q&A section, the format assistants most "
            "readily quote."
        ),
        evaluate_site=lambda c: (
            1.0 if c.has_faq_anywhere else 0.0,
            {"found": c.has_faq_anywhere},
        ),
    ),
    Signal(
        signal_id="answerability.question_headings",
        category=AiVisibilityCategory.ANSWERABILITY,
        scope="page",
        weight=2.5,
        title="question-style headings",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages use at least one "
            "question-style heading that mirrors how people ask an assistant."
        ),
        evaluate=lambda p, c: (
            1.0 if _has_question_heading(p) else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="answerability.faq_depth",
        category=AiVisibilityCategory.ANSWERABILITY,
        scope="page",
        weight=1.5,
        title="in-depth FAQ answers",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} FAQ pages carry three or "
            "more question/answer pairs, enough for an assistant to find a match."
        ),
        # Only pages that actually have an FAQ -- a site with none is already
        # scored by faq_present, so this must not penalise it a second time.
        applies=lambda p, c: p.has_faq_section,
        evaluate=lambda p, c: (
            1.0 if p.faq_question_count >= 3 else 0.0,
            {"faq_question_count": p.faq_question_count},
        ),
    ),
]


# ============================================================================
# SCHEMA -- machine-readable structured data (weight 0.16)
# ============================================================================
_SCHEMA_SIGNALS: list[Signal] = [
    Signal(
        signal_id="schema.structured_data_present",
        category=AiVisibilityCategory.SCHEMA,
        scope="page",
        weight=3.0,
        title="structured data (JSON-LD)",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages carry schema.org "
            "structured data an AI system can parse without guessing."
        ),
        evaluate=lambda p, c: (
            1.0 if p.has_schema_markup else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="schema.organization_entity",
        category=AiVisibilityCategory.SCHEMA,
        scope="site",
        weight=2.0,
        title="Organization/LocalBusiness schema",
        describe=lambda e: (
            "The site declares an Organization or LocalBusiness entity, so an "
            "assistant knows exactly which business it is describing."
            if e["found"]
            else "No Organization or LocalBusiness schema, so the business entity "
            "is left for the assistant to infer."
        ),
        evaluate_site=lambda c: (
            1.0 if (c.site_schema_types & _ORG_SCHEMA_TYPES) else 0.0,
            {"found": bool(c.site_schema_types & _ORG_SCHEMA_TYPES)},
        ),
    ),
    Signal(
        signal_id="schema.answer_rich_types",
        category=AiVisibilityCategory.SCHEMA,
        scope="site",
        weight=2.0,
        title="answer-rich schema types",
        describe=lambda e: (
            "The site uses answer-oriented schema types "
            f"({', '.join(e['types']) or 'none'}) such as FAQPage, HowTo, or "
            "Article that assistants extract structured answers from."
            if e["found"]
            else "No answer-oriented schema types (FAQPage, HowTo, Article, ...), "
            "which are the ones assistants lift structured answers from."
        ),
        evaluate_site=lambda c: (
            1.0 if (c.site_schema_types & _RICH_SCHEMA_TYPES) else 0.0,
            {
                "found": bool(c.site_schema_types & _RICH_SCHEMA_TYPES),
                "types": sorted(c.site_schema_types & _RICH_SCHEMA_TYPES),
            },
        ),
    ),
]


# ============================================================================
# STRUCTURE -- semantic organisation a parser can follow (weight 0.14)
# ============================================================================
_STRUCTURE_SIGNALS: list[Signal] = [
    Signal(
        signal_id="structure.clear_headings",
        category=AiVisibilityCategory.STRUCTURE,
        scope="page",
        weight=2.0,
        title="clear heading hierarchy",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages have a heading "
            "outline with a valid, unskipped hierarchy an AI can navigate."
        ),
        evaluate=lambda p, c: (
            1.0 if (p.heading_outline and p.heading_hierarchy_valid) else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="structure.scannable_blocks",
        category=AiVisibilityCategory.STRUCTURE,
        scope="page",
        weight=1.5,
        title="lists or tables",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages use lists or "
            "tables, the structured blocks assistants extract most cleanly."
        ),
        evaluate=lambda p, c: (
            1.0 if (p.has_list_content or p.has_table_content) else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="structure.single_main_heading",
        category=AiVisibilityCategory.STRUCTURE,
        scope="page",
        weight=1.5,
        title="one clear main heading",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages have exactly one "
            "H1, so the page's main topic is unambiguous."
        ),
        evaluate=lambda p, c: (
            1.0 if p.h1_count == 1 else 0.0,
            {"h1_count": p.h1_count},
        ),
    ),
]


# ============================================================================
# ENTITY_CLARITY -- is who/what/where unambiguous? (weight 0.12)
# ============================================================================
_ENTITY_CLARITY_SIGNALS: list[Signal] = [
    Signal(
        signal_id="entity_clarity.descriptive_titles",
        category=AiVisibilityCategory.ENTITY_CLARITY,
        scope="page",
        weight=2.0,
        title="descriptive page titles",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages have a descriptive "
            "title (10+ characters) that names what the page is about."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.title) and (p.title_length or 0) >= 10 else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="entity_clarity.meta_descriptions",
        category=AiVisibilityCategory.ENTITY_CLARITY,
        scope="page",
        weight=1.5,
        title="meta descriptions",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages provide a meta "
            "description, a ready-made summary an assistant can quote."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.meta_description) else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="entity_clarity.language_declared",
        category=AiVisibilityCategory.ENTITY_CLARITY,
        scope="page",
        weight=1.0,
        title="declared page language",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages declare their "
            "language, removing ambiguity for a multilingual model."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.lang) else 0.0,
            {},
        ),
    ),
]


# ============================================================================
# COMPLETENESS -- topical depth and coverage (weight 0.12)
# ============================================================================
_COMPLETENESS_SIGNALS: list[Signal] = [
    Signal(
        signal_id="completeness.substantial_content",
        category=AiVisibilityCategory.COMPLETENESS,
        scope="page",
        weight=2.5,
        title="substantial page content",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages have 300+ words, "
            "enough substance for an assistant to draw a real answer from."
        ),
        evaluate=lambda p, c: (
            1.0 if p.word_count >= 300 else 0.0,
            {"word_count": p.word_count},
        ),
    ),
    Signal(
        signal_id="completeness.in_depth_content",
        category=AiVisibilityCategory.COMPLETENESS,
        scope="page",
        weight=1.0,
        title="in-depth articles",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} substantial pages go "
            "beyond 800 words, the depth that answers follow-up questions too."
        ),
        # Only pages that already cleared the 300-word floor -- below that,
        # substantial_content already accounts for the shortfall.
        applies=lambda p, c: p.word_count >= 300,
        evaluate=lambda p, c: (
            1.0 if p.word_count >= 800 else 0.0,
            {"word_count": p.word_count},
        ),
    ),
    Signal(
        signal_id="completeness.topic_breadth",
        category=AiVisibilityCategory.COMPLETENESS,
        scope="site",
        weight=1.5,
        title="multiple content pages",
        describe=lambda e: (
            f"The site has {e['pages']} analysable pages, giving an assistant "
            "several angles on the business."
            if e["found"]
            else f"Only {e['pages']} analysable page(s); more topic coverage gives "
            "an assistant more to cite."
        ),
        evaluate_site=lambda c: (
            1.0 if c.analysable_page_count >= 3 else 0.0,
            {
                "found": c.analysable_page_count >= 3,
                "pages": c.analysable_page_count,
            },
        ),
    ),
]


# ============================================================================
# CITATION_READINESS -- can an AI attribute and reach this? (weight 0.10)
# ============================================================================
_CITATION_SIGNALS: list[Signal] = [
    Signal(
        signal_id="citation_readiness.authorship",
        category=AiVisibilityCategory.CITATION_READINESS,
        scope="page",
        weight=2.0,
        title="named authors",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages name an author, "
            "which an assistant needs to attribute a claim."
        ),
        evaluate=lambda p, c: (
            1.0 if p.has_author_info else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="citation_readiness.indexable",
        category=AiVisibilityCategory.CITATION_READINESS,
        scope="page",
        weight=1.5,
        title="indexable pages",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages allow indexing; a "
            "'noindex' page is invisible to the crawlers that feed AI answers."
        ),
        evaluate=lambda p, c: (
            0.0 if "noindex" in (p.meta_robots or "").lower() else 1.0,
            {"meta_robots": p.meta_robots or ""},
        ),
    ),
    Signal(
        signal_id="citation_readiness.canonical_url",
        category=AiVisibilityCategory.CITATION_READINESS,
        scope="page",
        weight=1.0,
        title="canonical URLs",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages declare a canonical "
            "URL, giving an assistant one stable address to cite."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.canonical_url) else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="citation_readiness.outbound_citations",
        category=AiVisibilityCategory.CITATION_READINESS,
        scope="page",
        weight=1.5,
        title="outbound citations",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages link out to "
            "external sources, a grounding signal that lends the content credibility."
        ),
        evaluate=lambda p, c: (
            1.0 if p.outbound_citation_count >= 1 else 0.0,
            {"outbound_citation_count": p.outbound_citation_count},
        ),
    ),
]


# ============================================================================
# FRESHNESS -- detectable recency (weight 0.08)
# ============================================================================
def _recent(page: WebsitePage, ctx: AiSiteContext) -> tuple[float, dict]:
    """Whether the page's newest date is within the recency window of as_of.

    Pages with no date never reach here (``applies`` filters them), so the newest
    date is present. A future date (ahead of ``as_of``) counts as fresh.
    """
    newest = _newest_date(page)
    assert newest is not None  # noqa: S101 - guaranteed by applies
    age_days = (_as_utc(ctx.as_of) - newest).days
    return (
        1.0 if age_days <= _RECENCY_DAYS else 0.0,
        {"age_days": age_days, "window_days": _RECENCY_DAYS},
    )


_FRESHNESS_SIGNALS: list[Signal] = [
    Signal(
        signal_id="freshness.dates_present",
        category=AiVisibilityCategory.FRESHNESS,
        scope="page",
        weight=2.0,
        title="publish or update dates",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} pages expose a published "
            "or modified date, which assistants weigh when judging currency."
        ),
        evaluate=lambda p, c: (
            1.0 if _newest_date(p) is not None else 0.0,
            {},
        ),
    ),
    Signal(
        signal_id="freshness.recent_updates",
        category=AiVisibilityCategory.FRESHNESS,
        scope="page",
        weight=1.0,
        title="recently updated content",
        describe=lambda e: (
            f"{e['pages_passing']} of {e['pages_checked']} dated pages were updated "
            "within about two years, so the content reads as current."
        ),
        # Only pages that carry a date at all -- undated pages are dates_present's
        # concern, not this one.
        applies=lambda p, c: _newest_date(p) is not None,
        evaluate=_recent,
    ),
]


# ============================================================================
# TRUST -- business legitimacy signals (weight 0.10)
# ============================================================================
_TRUST_SIGNALS: list[Signal] = [
    Signal(
        signal_id="trust.contact_info",
        category=AiVisibilityCategory.TRUST,
        scope="site",
        weight=2.0,
        title="public contact details",
        describe=lambda e: (
            "The site publishes contact details, a basic legitimacy signal for a "
            "real business."
            if e["found"]
            else "No contact details were found; assistants favour sources that "
            "look like a reachable, real business."
        ),
        evaluate_site=lambda c: (
            1.0 if c.has_contact_info_anywhere else 0.0,
            {"found": c.has_contact_info_anywhere},
        ),
    ),
    Signal(
        signal_id="trust.secure_connection",
        category=AiVisibilityCategory.TRUST,
        scope="site",
        weight=1.0,
        title="secure HTTPS connection",
        describe=lambda e: (
            "The site is served over HTTPS."
            if e["https"]
            else "The site is served over plain HTTP, a trust and security "
            "downgrade that crawlers note."
        ),
        evaluate_site=lambda c: (
            1.0 if c.is_https else 0.0,
            {"https": c.is_https},
        ),
    ),
]


# The full signal set, grouped by category for readability but flattened for use.
SIGNALS: tuple[Signal, ...] = tuple(
    _ANSWERABILITY_SIGNALS
    + _SCHEMA_SIGNALS
    + _STRUCTURE_SIGNALS
    + _ENTITY_CLARITY_SIGNALS
    + _COMPLETENESS_SIGNALS
    + _CITATION_SIGNALS
    + _FRESHNESS_SIGNALS
    + _TRUST_SIGNALS
)

# The maximum raw points a category can earn -- the sum of its signal weights.
# Used only when a category has NO applicable signal (a degenerate crawl): the
# engine awards full marks against this nominal maximum rather than dividing by
# zero or writing a points_available of 0, which the DB CHECK forbids.
CATEGORY_MAX_POINTS: dict[AiVisibilityCategory, float] = {
    category: sum(s.weight for s in SIGNALS if s.category is category)
    for category in AiVisibilityCategory
}


def _validate_signals() -> None:
    """Fail fast at import if the signal set is internally inconsistent.

    A duplicated signal_id, a weight table that no longer sums to 1.0, or a
    category with no signals would silently corrupt every score; better to refuse
    to start than to serve a wrong number.
    """
    ids = [s.signal_id for s in SIGNALS]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate AI-visibility signal_id(s): {dupes}")

    total_weight = sum(CATEGORY_WEIGHTS.values())
    if abs(total_weight - 1.0) > 1e-9:
        raise ValueError(f"CATEGORY_WEIGHTS must sum to 1.0, got {total_weight}")

    if set(CATEGORY_WEIGHTS) != set(AiVisibilityCategory):
        raise ValueError(
            "CATEGORY_WEIGHTS must cover exactly the AiVisibilityCategory values"
        )

    for category in AiVisibilityCategory:
        # Every category must have at least one signal, so its nominal maximum is
        # positive -- AiVisibilityCategoryScore CHECKs points_available > 0.
        if CATEGORY_MAX_POINTS[category] <= 0:
            raise ValueError(f"category {category.value} has no signals")

    for signal in SIGNALS:
        if signal.scope == "page" and signal.evaluate is None:
            raise ValueError(f"page signal {signal.signal_id} has no evaluate()")
        if signal.scope == "site" and signal.evaluate_site is None:
            raise ValueError(f"site signal {signal.signal_id} has no evaluate_site()")
        if signal.weight <= 0:
            raise ValueError(f"signal {signal.signal_id} must have a positive weight")


_validate_signals()


__all__ = [
    "CATEGORY_MAX_POINTS",
    "CATEGORY_WEIGHTS",
    "ENGINE_VERSION",
    "SIGNALS",
    "AiSiteContext",
    "Signal",
]
