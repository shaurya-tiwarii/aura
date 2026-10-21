"""The SEO scoring ruleset: every deduction AURA can make, in one auditable place.

This module is deliberately declarative. Each :class:`Rule` names a stable
``rule_id`` (e.g. ``metadata.title_missing``), the category it belongs to, the
points it is worth, the severity of the issue it raises, and -- crucially -- the
exact condition that makes it fire, expressed as a small pure function over a
crawled page's own signals. Nothing here is heuristic hand-waving: a reader can
point at any rule and see the threshold that produced a deduction, which is the
explainability requirement of spec section 24.

Scoring model, so the numbers reconcile exactly (:mod:`app.engines.seo.engine`
does the arithmetic):

* A **page-scoped** rule is evaluated on every page it *applies* to and returns a
  fraction in ``[0, 1]`` -- ``1.0`` is full marks, ``0.0`` is a total miss, and a
  value between is partial credit (only the image-alt rule uses the middle today).
  The rule's contribution is ``weight x mean(fraction across applicable pages)``.
  A page the rule does not apply to (a length rule on a page with no title) is
  excluded from that mean and from the category's available points -- you are
  never penalised twice for one absence.
* A **site-scoped** rule is evaluated once against the whole-site context and
  returns ``0.0`` or ``1.0``.

The weight a rule carries is *within* its category. A category's available points
are the sum of its applicable rule weights; the category's earned points are that
sum minus every point deducted by an issue in it -- so summing the issues always
reconstructs the category score, and summing the weighted category scores
reconstructs the headline. See :data:`CATEGORY_WEIGHTS` for how the eight
categories combine into the 0-100 result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.models.enums import SeoCategory, Severity

if TYPE_CHECKING:
    from app.models.website import WebsitePage

# The scoring ruleset version. Baked into every report so the improvement loop
# refuses to compare two scores produced by different rules (a weight change
# would otherwise look like a real-world improvement). Bump on any change to a
# weight, threshold, or the rule set below.
ENGINE_VERSION = "DVO-SEO-SCORE-V2.0"

# Each category's share of the headline score. These are the spec's eight SEO
# categories (section 6); the weights encode their relative influence on search
# visibility for a small-business site -- metadata and content lead because they
# are the levers a small business can actually pull, images and readability trail
# because they are refinements. They MUST sum to 1.0 (asserted at import), so the
# weighted average of the per-category 0-100 scores is itself a 0-100 score.
CATEGORY_WEIGHTS: dict[SeoCategory, float] = {
    SeoCategory.METADATA: 0.18,
    SeoCategory.CONTENT: 0.16,
    SeoCategory.TECHNICAL: 0.14,
    SeoCategory.STRUCTURE: 0.14,
    SeoCategory.LINKS: 0.12,
    SeoCategory.SCHEMA: 0.10,
    SeoCategory.IMAGES: 0.08,
    SeoCategory.READABILITY: 0.08,
}


@dataclass(frozen=True)
class SiteContext:
    """Whole-site facts a rule needs that are not on a single page.

    Assembled once by the engine from the scan's stored aggregates and the set of
    crawled pages, so a site-scoped rule (is the site on HTTPS, is there a
    sitemap) and the page-scoped rules that need site context (is this an
    orphan page on a multi-page site) read from the same snapshot.
    """

    is_https: bool
    robots_found: bool
    sitemap_found: bool
    analysable_page_count: int
    keywords: tuple[str, ...]
    # Lower-cased union of every schema.org type seen anywhere on the site, so an
    # Organization block on the homepage credits the whole site.
    site_schema_types: frozenset[str]


# A page/site evaluator returns (fraction_in_0_to_1, evidence_dict). The evidence
# is the machine-readable "why": the values that decided the fraction, stored on
# the issue verbatim so the UI shows "title is 12 chars (recommended 30-60)".
PageEval = Callable[["WebsitePage", SiteContext], "tuple[float, dict]"]
SiteEval = Callable[[SiteContext], "tuple[float, dict]"]
PageApplies = Callable[["WebsitePage", SiteContext], bool]
Describe = Callable[[dict], str]


@dataclass(frozen=True)
class Rule:
    """One scoring check.

    ``scope`` selects which evaluator the engine calls. A ``page`` rule provides
    ``applies`` (default: every analysable page) and ``evaluate``; a ``site`` rule
    provides ``evaluate_site``. ``describe`` turns an evidence dict into the
    human-readable sentence stored on the issue.
    """

    rule_id: str
    category: SeoCategory
    scope: Literal["page", "site"]
    weight: float
    severity: Severity
    title: str
    fix: str
    describe: Describe
    applies: PageApplies | None = None
    evaluate: PageEval | None = None
    evaluate_site: SiteEval | None = None
    # Extra fields excluded from equality so two rules are never accidentally equal
    _tags: tuple[str, ...] = field(default=(), compare=False)


def _text(value: str | None) -> str:
    """A stripped string, treating None/whitespace-only as empty."""
    return (value or "").strip()


# ============================================================================
# METADATA -- the title/description/head signals (weight 0.18)
# ============================================================================
_METADATA_RULES: list[Rule] = [
    Rule(
        rule_id="metadata.title_missing",
        category=SeoCategory.METADATA,
        scope="page",
        weight=3.0,
        severity=Severity.HIGH,
        title="Missing page title",
        fix="Add a unique <title> tag of about 30-60 characters describing the page.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have no <title> tag."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.title) else 0.0,
            {"found": bool(_text(p.title))},
        ),
    ),
    Rule(
        rule_id="metadata.title_length",
        category=SeoCategory.METADATA,
        scope="page",
        weight=2.0,
        severity=Severity.MEDIUM,
        title="Title length outside the recommended range",
        fix="Aim for 30-60 characters so the title is neither truncated nor too thin.",
        describe=lambda e: (
            f"Title is {e['length']} characters (recommended 30-60); "
            f"{e['pages_affected']} of {e['pages_checked']} titled pages are off-range."
        ),
        # Only pages that HAVE a title -- a missing title is title_missing's job.
        applies=lambda p, c: bool(_text(p.title)),
        evaluate=lambda p, c: (
            1.0 if 30 <= (p.title_length or 0) <= 60 else 0.0,
            {
                "length": p.title_length or 0,
                "recommended_min": 30,
                "recommended_max": 60,
            },
        ),
    ),
    Rule(
        rule_id="metadata.description_missing",
        category=SeoCategory.METADATA,
        scope="page",
        weight=3.0,
        severity=Severity.MEDIUM,
        title="Missing meta description",
        fix="Add a meta description of about 70-160 characters to improve click-through.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have no meta description."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.meta_description) else 0.0,
            {"found": bool(_text(p.meta_description))},
        ),
    ),
    Rule(
        rule_id="metadata.description_length",
        category=SeoCategory.METADATA,
        scope="page",
        weight=2.0,
        severity=Severity.LOW,
        title="Meta description length outside the recommended range",
        fix="Aim for 70-160 characters so search engines show it without truncating.",
        describe=lambda e: (
            f"Meta description is {e['length']} characters (recommended 70-160); "
            f"{e['pages_affected']} of {e['pages_checked']} pages are off-range."
        ),
        applies=lambda p, c: bool(_text(p.meta_description)),
        evaluate=lambda p, c: (
            1.0 if 70 <= (p.meta_description_length or 0) <= 160 else 0.0,
            {
                "length": p.meta_description_length or 0,
                "recommended_min": 70,
                "recommended_max": 160,
            },
        ),
    ),
    Rule(
        rule_id="metadata.viewport_missing",
        category=SeoCategory.METADATA,
        scope="page",
        weight=1.0,
        severity=Severity.MEDIUM,
        title="No mobile viewport tag",
        fix='Add <meta name="viewport" content="width=device-width, initial-scale=1">.',
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages lack a viewport meta "
            "tag, so they may not render correctly on mobile."
        ),
        evaluate=lambda p, c: (
            1.0 if p.has_viewport_meta else 0.0,
            {"found": p.has_viewport_meta},
        ),
    ),
    Rule(
        rule_id="metadata.open_graph_missing",
        category=SeoCategory.METADATA,
        scope="page",
        weight=1.0,
        severity=Severity.LOW,
        title="No Open Graph tags",
        fix="Add og:title, og:description, and og:image so shared links preview well.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have no Open Graph "
            "tags, so links shared to social platforms show no preview."
        ),
        evaluate=lambda p, c: (
            1.0 if p.has_open_graph else 0.0,
            {"found": p.has_open_graph},
        ),
    ),
]


# ============================================================================
# CONTENT -- depth and keyword coverage (weight 0.16)
# ============================================================================
_CONTENT_RULES: list[Rule] = [
    Rule(
        rule_id="content.thin_content",
        category=SeoCategory.CONTENT,
        scope="page",
        weight=3.0,
        severity=Severity.HIGH,
        title="Thin content",
        fix="Expand the page past ~300 words of genuinely useful copy.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have under "
            f"{e['recommended_min']} words (this page: {e['word_count']})."
        ),
        evaluate=lambda p, c: (
            1.0 if p.word_count >= 150 else 0.0,
            {"word_count": p.word_count, "recommended_min": 150},
        ),
    ),
    Rule(
        rule_id="content.low_word_count",
        category=SeoCategory.CONTENT,
        scope="page",
        weight=1.0,
        severity=Severity.LOW,
        title="Below the recommended word count",
        fix="Aim for 300+ words so the page has enough depth to rank.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have 150-299 words "
            f"(recommended {e['recommended_min']}+)."
        ),
        # Only pages that cleared the thin-content floor -- below 150 is thin_content's.
        applies=lambda p, c: p.word_count >= 150,
        evaluate=lambda p, c: (
            1.0 if p.word_count >= 300 else 0.0,
            {"word_count": p.word_count, "recommended_min": 300},
        ),
    ),
    Rule(
        rule_id="content.no_target_keywords",
        category=SeoCategory.CONTENT,
        scope="page",
        weight=2.0,
        severity=Severity.MEDIUM,
        title="Target keywords not used on the page",
        fix="Work the business's target keywords naturally into headings and body copy.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages use none of the "
            f"{e['keywords_checked']} target keywords."
        ),
        # Only when the business supplied keywords and the page has body text.
        applies=lambda p, c: bool(c.keywords) and p.word_count > 0,
        evaluate=lambda p, c: (
            1.0 if sum((p.keyword_usage or {}).values()) > 0 else 0.0,
            {
                "keywords_checked": len(c.keywords),
                "occurrences": sum((p.keyword_usage or {}).values()),
            },
        ),
    ),
]


# ============================================================================
# TECHNICAL -- indexability and crawl infrastructure (weight 0.14)
# ============================================================================
_TECHNICAL_RULES: list[Rule] = [
    Rule(
        rule_id="technical.not_https",
        category=SeoCategory.TECHNICAL,
        scope="site",
        weight=3.0,
        severity=Severity.HIGH,
        title="Site is not served over HTTPS",
        fix="Install a TLS certificate and redirect all HTTP traffic to HTTPS.",
        describe=lambda e: "The site is served over HTTP, not HTTPS.",
        evaluate_site=lambda c: (1.0 if c.is_https else 0.0, {"https": c.is_https}),
    ),
    Rule(
        rule_id="technical.no_robots_txt",
        category=SeoCategory.TECHNICAL,
        scope="site",
        weight=1.0,
        severity=Severity.LOW,
        title="No robots.txt found",
        fix="Add a robots.txt at the site root, even a permissive one, and link the sitemap.",
        describe=lambda e: "No robots.txt was found at the site root.",
        evaluate_site=lambda c: (
            1.0 if c.robots_found else 0.0,
            {"found": c.robots_found},
        ),
    ),
    Rule(
        rule_id="technical.no_sitemap",
        category=SeoCategory.TECHNICAL,
        scope="site",
        weight=2.0,
        severity=Severity.MEDIUM,
        title="No XML sitemap found",
        fix="Publish an XML sitemap and reference it from robots.txt so crawlers find every page.",
        describe=lambda e: "No XML sitemap was found at any conventional location.",
        evaluate_site=lambda c: (
            1.0 if c.sitemap_found else 0.0,
            {"found": c.sitemap_found},
        ),
    ),
    Rule(
        rule_id="technical.noindex",
        category=SeoCategory.TECHNICAL,
        scope="page",
        weight=3.0,
        severity=Severity.CRITICAL,
        title="Page asks search engines not to index it",
        fix="Remove 'noindex' from the meta robots tag unless the page is deliberately hidden.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages carry a 'noindex' "
            "directive and will be excluded from search results entirely."
        ),
        evaluate=lambda p, c: (
            0.0 if "noindex" in (p.meta_robots or "").lower() else 1.0,
            {"meta_robots": p.meta_robots or ""},
        ),
    ),
    Rule(
        rule_id="technical.canonical_missing",
        category=SeoCategory.TECHNICAL,
        scope="page",
        weight=2.0,
        severity=Severity.LOW,
        title="No canonical URL",
        fix='Add a <link rel="canonical"> so duplicate URLs consolidate ranking signals.',
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages declare no canonical URL."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.canonical_url) else 0.0,
            {"found": bool(_text(p.canonical_url))},
        ),
    ),
    Rule(
        rule_id="technical.lang_missing",
        category=SeoCategory.TECHNICAL,
        scope="page",
        weight=1.0,
        severity=Severity.LOW,
        title="No language declared",
        fix='Set the page language, e.g. <html lang="en">.',
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages declare no lang attribute."
        ),
        evaluate=lambda p, c: (
            1.0 if _text(p.lang) else 0.0,
            {"found": bool(_text(p.lang))},
        ),
    ),
    Rule(
        rule_id="technical.slow_response",
        category=SeoCategory.TECHNICAL,
        scope="page",
        weight=1.0,
        severity=Severity.LOW,
        title="Slow server response",
        fix="Reduce time-to-first-byte below ~2s (caching, faster hosting, lighter pages).",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages responded slower than "
            f"{e['threshold_ms']}ms (this page: {e['response_time_ms']}ms)."
        ),
        # Only pages we actually timed.
        applies=lambda p, c: p.response_time_ms is not None,
        evaluate=lambda p, c: (
            1.0 if (p.response_time_ms or 0) <= 2000 else 0.0,
            {"response_time_ms": p.response_time_ms or 0, "threshold_ms": 2000},
        ),
    ),
]


# ============================================================================
# STRUCTURE -- heading hierarchy (weight 0.14)
# ============================================================================
_STRUCTURE_RULES: list[Rule] = [
    Rule(
        rule_id="structure.h1_missing",
        category=SeoCategory.STRUCTURE,
        scope="page",
        weight=3.0,
        severity=Severity.HIGH,
        title="No H1 heading",
        fix="Add exactly one H1 that states what the page is about.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have no H1 heading."
        ),
        evaluate=lambda p, c: (
            1.0 if p.h1_count >= 1 else 0.0,
            {"h1_count": p.h1_count},
        ),
    ),
    Rule(
        rule_id="structure.multiple_h1",
        category=SeoCategory.STRUCTURE,
        scope="page",
        weight=2.0,
        severity=Severity.MEDIUM,
        title="More than one H1 heading",
        fix="Use a single H1 per page; demote the extras to H2.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages have more than one H1 "
            f"(this page: {e['h1_count']})."
        ),
        applies=lambda p, c: p.h1_count >= 1,
        evaluate=lambda p, c: (
            1.0 if p.h1_count == 1 else 0.0,
            {"h1_count": p.h1_count},
        ),
    ),
    Rule(
        rule_id="structure.heading_hierarchy",
        category=SeoCategory.STRUCTURE,
        scope="page",
        weight=2.0,
        severity=Severity.MEDIUM,
        title="Skipped heading level",
        fix="Don't jump heading levels (e.g. H1 straight to H3); keep them sequential.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages skip a heading level."
        ),
        # Only pages with a clean single H1 -- otherwise h1_missing/multiple_h1 already
        # explain why the hierarchy is invalid, and this would double-count.
        applies=lambda p, c: p.h1_count == 1,
        evaluate=lambda p, c: (
            1.0 if p.heading_hierarchy_valid else 0.0,
            {"heading_hierarchy_valid": p.heading_hierarchy_valid},
        ),
    ),
    Rule(
        rule_id="structure.no_subheadings",
        category=SeoCategory.STRUCTURE,
        scope="page",
        weight=1.0,
        severity=Severity.LOW,
        title="Long page with no subheadings",
        fix="Break long copy into sections with H2/H3 subheadings.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} long pages "
            f"({e['word_count']}+ words) use no H2 subheadings."
        ),
        # Only long pages -- a short page legitimately needs no subheadings.
        applies=lambda p, c: p.word_count >= 300,
        evaluate=lambda p, c: (
            1.0 if p.h2_count >= 1 else 0.0,
            {"h2_count": p.h2_count, "word_count": p.word_count},
        ),
    ),
]


# ============================================================================
# LINKS -- internal linking and broken links (weight 0.12)
# ============================================================================
_LINKS_RULES: list[Rule] = [
    Rule(
        rule_id="links.broken_links",
        category=SeoCategory.LINKS,
        scope="page",
        weight=4.0,
        severity=Severity.HIGH,
        title="Broken links",
        fix="Fix or remove links that returned a 4xx/5xx status.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages contain links that "
            f"failed to resolve (this page: {e['broken_link_count']})."
        ),
        evaluate=lambda p, c: (
            1.0 if p.broken_link_count == 0 else 0.0,
            {"broken_link_count": p.broken_link_count},
        ),
    ),
    Rule(
        rule_id="links.no_internal_links",
        category=SeoCategory.LINKS,
        scope="page",
        weight=3.0,
        severity=Severity.MEDIUM,
        title="Orphan page (no internal links out)",
        fix="Link each page to related pages so crawlers and readers can navigate.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages link to no other page "
            "on the site."
        ),
        # Meaningless on a single-page site -- there is nowhere to link.
        applies=lambda p, c: c.analysable_page_count > 1,
        evaluate=lambda p, c: (
            1.0 if p.internal_link_count > 0 else 0.0,
            {"internal_link_count": p.internal_link_count},
        ),
    ),
]


# ============================================================================
# SCHEMA -- structured data (weight 0.10)
# ============================================================================
_ORG_SCHEMA_TYPES = frozenset({"organization", "localbusiness", "corporation", "store"})

_SCHEMA_RULES: list[Rule] = [
    Rule(
        rule_id="schema.no_structured_data",
        category=SeoCategory.SCHEMA,
        scope="page",
        weight=3.0,
        severity=Severity.MEDIUM,
        title="No structured data",
        fix="Add JSON-LD schema.org markup describing the page's content.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} pages carry no schema.org "
            "structured data."
        ),
        evaluate=lambda p, c: (
            1.0 if p.has_schema_markup else 0.0,
            {"found": p.has_schema_markup},
        ),
    ),
    Rule(
        rule_id="schema.no_organization",
        category=SeoCategory.SCHEMA,
        scope="site",
        weight=2.0,
        severity=Severity.MEDIUM,
        title="No Organization or LocalBusiness schema",
        fix="Add Organization (or LocalBusiness) JSON-LD so search engines recognise the business entity.",
        describe=lambda e: (
            "The site has no Organization or LocalBusiness schema, which search engines "
            "use to build the business's knowledge panel."
        ),
        evaluate_site=lambda c: (
            1.0 if (c.site_schema_types & _ORG_SCHEMA_TYPES) else 0.0,
            {"found": bool(c.site_schema_types & _ORG_SCHEMA_TYPES)},
        ),
    ),
]


# ============================================================================
# IMAGES -- alt-text coverage (weight 0.08)
# ============================================================================
def _alt_coverage(page: WebsitePage, _c: SiteContext) -> tuple[float, dict]:
    """Fraction of a page's images that have alt text (graded, not pass/fail).

    A page with 8 of 10 images described scores 0.8, not 0 -- partial credit that
    a binary rule would throw away. Pages with no images never reach here
    (``applies`` filters them), so the division is safe.
    """
    total = page.image_count
    described = total - page.images_missing_alt
    return described / total, {
        "image_count": total,
        "images_missing_alt": page.images_missing_alt,
    }


_IMAGES_RULES: list[Rule] = [
    Rule(
        rule_id="images.missing_alt",
        category=SeoCategory.IMAGES,
        scope="page",
        weight=4.0,
        severity=Severity.MEDIUM,
        title="Images missing alt text",
        fix="Add descriptive alt text to every meaningful image (empty alt only for decorative ones).",
        describe=lambda e: (
            f"{e['images_missing_alt']} image(s) on this page have no alt text; "
            f"{e['pages_affected']} of {e['pages_checked']} pages with images are affected."
        ),
        # Only pages that actually have images.
        applies=lambda p, c: p.image_count > 0,
        evaluate=_alt_coverage,
    ),
]


# ============================================================================
# READABILITY -- Flesch reading ease and grade level (weight 0.08)
# ============================================================================
_READABILITY_RULES: list[Rule] = [
    Rule(
        rule_id="readability.hard_to_read",
        category=SeoCategory.READABILITY,
        scope="page",
        weight=3.0,
        severity=Severity.MEDIUM,
        title="Difficult to read",
        fix="Shorten sentences and prefer plain words; aim for a Flesch score above 50.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} scorable pages read as "
            f"difficult (Flesch below {e['threshold']}; this page: {e['readability_score']})."
        ),
        # Only pages long enough for a meaningful readability score.
        applies=lambda p, c: p.readability_score is not None,
        evaluate=lambda p, c: (
            1.0 if (p.readability_score or 0) >= 40 else 0.0,
            {"readability_score": round(p.readability_score or 0, 1), "threshold": 40},
        ),
    ),
    Rule(
        rule_id="readability.grade_too_high",
        category=SeoCategory.READABILITY,
        scope="page",
        weight=1.0,
        severity=Severity.LOW,
        title="Reading level too advanced",
        fix="Simplify wording so a general audience (grade ~8-12) can follow it.",
        describe=lambda e: (
            f"{e['pages_affected']} of {e['pages_checked']} scorable pages need a reading "
            f"level above grade {e['threshold']} (this page: {e['reading_grade_level']})."
        ),
        applies=lambda p, c: p.reading_grade_level is not None,
        evaluate=lambda p, c: (
            1.0 if (p.reading_grade_level or 0) <= 14 else 0.0,
            {
                "reading_grade_level": round(p.reading_grade_level or 0, 1),
                "threshold": 14,
            },
        ),
    ),
]


# The full ruleset, grouped by category for readability but flattened for use.
RULES: tuple[Rule, ...] = tuple(
    _METADATA_RULES
    + _CONTENT_RULES
    + _TECHNICAL_RULES
    + _STRUCTURE_RULES
    + _LINKS_RULES
    + _SCHEMA_RULES
    + _IMAGES_RULES
    + _READABILITY_RULES
)

# The maximum raw points a category can lose -- the sum of its rule weights. Used
# only when a category has NO applicable evidence (e.g. a site with zero images):
# the engine reports full marks against this nominal maximum rather than dividing
# by zero or silently dropping the category and re-weighting the others.
CATEGORY_MAX_POINTS: dict[SeoCategory, float] = {
    category: sum(r.weight for r in RULES if r.category is category)
    for category in SeoCategory
}


def _validate_ruleset() -> None:
    """Fail fast at import if the ruleset is internally inconsistent.

    A typo'd duplicate rule_id or a weight table that no longer sums to 1.0 would
    silently corrupt every score; better to refuse to start.
    """
    ids = [r.rule_id for r in RULES]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate SEO rule_id(s): {dupes}")

    total_weight = sum(CATEGORY_WEIGHTS.values())
    if abs(total_weight - 1.0) > 1e-9:
        raise ValueError(f"CATEGORY_WEIGHTS must sum to 1.0, got {total_weight}")

    if set(CATEGORY_WEIGHTS) != set(SeoCategory):
        raise ValueError(
            "CATEGORY_WEIGHTS must cover exactly the eight SeoCategory values"
        )

    for rule in RULES:
        if rule.scope == "page" and rule.evaluate is None:
            raise ValueError(f"page rule {rule.rule_id} has no evaluate()")
        if rule.scope == "site" and rule.evaluate_site is None:
            raise ValueError(f"site rule {rule.rule_id} has no evaluate_site()")
        if rule.weight <= 0:
            raise ValueError(f"rule {rule.rule_id} must have a positive weight")


_validate_ruleset()


__all__ = [
    "CATEGORY_MAX_POINTS",
    "CATEGORY_WEIGHTS",
    "ENGINE_VERSION",
    "RULES",
    "Rule",
    "SiteContext",
]
