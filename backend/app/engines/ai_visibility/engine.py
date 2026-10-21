"""The AI-visibility scoring engine: crawled pages -> an explainable 0-100 score.

Sibling of :mod:`app.engines.seo.engine`, same pure-function discipline: no
database session, no language model, no wall-clock reads (the reference instant
arrives as ``as_of``). Give it a scan's pages plus a handful of whole-site facts
and it returns an :class:`AiVisibilityResult` the service layer persists verbatim.

Unlike the SEO engine there are **no issues** -- AI-visibility is a set of positive
signals a page either carries or does not, not a list of mistakes to deduct for.
So a category's score is simply how much of its available signal weight the pages
earned, and the result reconciles the same way the SEO one does (the verifier
checks each identity against the stored rows):

* per signal, ``points == round(weight * fraction, 4)``;
* per category, ``points_earned == round(sum(signal points), 4)`` and
  ``normalized_score == round(points_earned / points_available * 100, 1)``;
* overall, ``overall_score == round(sum(category.normalized_score * weight), 1)``.

The number is an *estimate of AI-search readiness*, never a claim about how any
model ranks or cites the site (spec sections 7 and 45). The engine only stamps the
version and the categories; the human-facing disclaimer lives on the report row
the service writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.engines.ai_visibility.signals import (
    _ORG_SCHEMA_TYPES,
    CATEGORY_MAX_POINTS,
    CATEGORY_WEIGHTS,
    ENGINE_VERSION,
    SIGNALS,
    AiSiteContext,
    Signal,
)
from app.models.enums import AiVisibilityCategory, ScoreGrade

if TYPE_CHECKING:
    from app.models.website import WebsitePage

# Match the SEO engine's rounding so the two scores reconcile identically: raw
# point values to 4 decimals (keeps the sum identities exact under float), the two
# display scores to 1 decimal (what the model stores and the UI shows).
_POINTS_DP = 4
_SCORE_DP = 1


@dataclass(frozen=True)
class AiCategoryResult:
    """One weighted category, ready to become an ``AiVisibilityCategoryScore`` row.

    ``signal_results`` records every signal the category ran -- passed, failed, or
    not applicable -- so the category score is auditable without re-running the
    engine, exactly as ``check_results`` does for SEO.
    """

    category: str
    points_earned: float
    points_available: float
    normalized_score: float
    weight: float
    explanation: str
    signal_results: dict


@dataclass(frozen=True)
class AiVisibilityResult:
    overall_score: float
    grade: str
    points_earned: float
    points_available: float
    pages_analyzed: int
    engine_version: str
    summary: str
    # The four denormalised flags the report row carries for fast filtering.
    has_faq_content: bool
    has_structured_data: bool
    has_clear_entity_info: bool
    has_freshness_signals: bool
    categories: list[AiCategoryResult] = field(default_factory=list)


def _analysable(pages: Sequence[WebsitePage]) -> list[WebsitePage]:
    """Pages that yielded HTML, in a deterministic order (homepage, depth, URL).

    Identical policy to the SEO engine: failed fetches carry no signals and are
    excluded from every average, and the stable sort makes the whole result
    reproducible regardless of row arrival order.
    """
    fetched = [p for p in pages if p.was_fetched]
    return sorted(fetched, key=lambda p: (not p.is_homepage, p.depth, p.normalized_url))


def _build_context(
    pages: Sequence[WebsitePage],
    *,
    site_url: str,
    as_of: datetime,
) -> AiSiteContext:
    schema_types: set[str] = set()
    for page in pages:
        for value in page.schema_types or []:
            schema_types.add(str(value).lower())
    return AiSiteContext(
        is_https=urlsplit(site_url).scheme == "https",
        analysable_page_count=len(pages),
        as_of=as_of,
        site_schema_types=frozenset(schema_types),
        has_faq_anywhere=any(p.has_faq_section for p in pages),
        has_contact_info_anywhere=any(p.has_contact_info for p in pages),
    )


def _score_category(
    category: AiVisibilityCategory,
    signals: list[Signal],
    pages: list[WebsitePage],
    ctx: AiSiteContext,
) -> AiCategoryResult:
    """Score one category by summing its applicable signals' earned points.

    A site signal runs once against the context; a page signal runs on every page
    it applies to and earns ``weight * (share of those pages that pass)``. A signal
    that applies to no page (an FAQ-depth check where nothing has an FAQ) is
    recorded inactive and excluded from the totals -- it is not a zero, it is a
    non-event.
    """
    available = 0.0
    earned = 0.0
    signal_results: dict[str, dict] = {}
    present: list[str] = []
    weak: list[str] = []
    active_any = False

    for signal in signals:
        if signal.scope == "site":
            assert signal.evaluate_site is not None  # noqa: S101 - guaranteed by _validate_signals
            fraction, evidence = signal.evaluate_site(ctx)
            agg_evidence = dict(evidence)
        else:
            assert signal.evaluate is not None  # noqa: S101 - guaranteed by _validate_signals
            applicable = [
                p for p in pages if signal.applies is None or signal.applies(p, ctx)
            ]
            if not applicable:
                signal_results[signal.signal_id] = {
                    "passed": True,
                    "points": 0.0,
                    "max": 0.0,
                    "applicable": False,
                    "fraction": 1.0,
                    "detail": "Not applicable to any analysed page.",
                    "evidence": {},
                }
                continue
            per_page = [signal.evaluate(p, ctx)[0] for p in applicable]
            passing = sum(1 for f in per_page if f >= 1.0)
            fraction = passing / len(applicable)
            agg_evidence = {
                "pages_passing": passing,
                "pages_checked": len(applicable),
            }

        active_any = True
        available += signal.weight
        points = round(signal.weight * fraction, _POINTS_DP)
        earned += points
        passed = fraction >= 1.0
        signal_results[signal.signal_id] = {
            "passed": passed,
            "points": points,
            "max": signal.weight,
            "applicable": True,
            "fraction": round(fraction, _POINTS_DP),
            "detail": signal.describe(agg_evidence),
            "evidence": agg_evidence,
        }
        (present if passed else weak).append(signal.title)

    if not active_any:
        # No signal in this category applied to anything -- a degenerate crawl. Award
        # full marks against the nominal maximum rather than dividing by zero or
        # writing points_available == 0 (which AiVisibilityCategoryScore's CHECK
        # forbids). In practice every category has an always-applicable signal, so
        # this branch is a guard, not a normal path.
        nominal = CATEGORY_MAX_POINTS[category]
        for signal in signals:
            signal_results[signal.signal_id] = {
                "passed": True,
                "points": signal.weight,
                "max": signal.weight,
                "applicable": False,
                "fraction": 1.0,
                "detail": "No signal in this category applied to the crawled pages.",
                "evidence": {},
            }
        return AiCategoryResult(
            category=category.value,
            points_earned=nominal,
            points_available=nominal,
            normalized_score=100.0,
            weight=CATEGORY_WEIGHTS[category],
            explanation="No applicable signals for this category on the crawled site.",
            signal_results=signal_results,
        )

    earned = round(earned, _POINTS_DP)
    normalized = round(earned / available * 100, _SCORE_DP)
    return AiCategoryResult(
        category=category.value,
        points_earned=earned,
        points_available=round(available, _POINTS_DP),
        normalized_score=normalized,
        weight=CATEGORY_WEIGHTS[category],
        explanation=_explain(present, weak),
        signal_results=signal_results,
    )


def _explain(present: list[str], weak: list[str]) -> str:
    """A deterministic per-category sentence -- generated here, never by the AI.

    Rolls the category's signal titles into what is present versus missing so a
    reader (and the verifier) can see exactly which checks drove the number, per
    the explainability requirement (spec section 24).
    """
    parts: list[str] = []
    if present:
        parts.append("Present: " + ", ".join(present) + ".")
    if weak:
        parts.append("Missing or weak: " + ", ".join(weak) + ".")
    if not parts:
        return "No signals evaluated."
    return " ".join(parts)


def _summarise(
    overall: float,
    grade: str,
    pages_analyzed: int,
    categories: list[AiCategoryResult],
) -> str:
    """A deterministic one-paragraph summary -- generated here, never by the AI."""
    ranked = sorted(categories, key=lambda c: c.normalized_score)
    weakest = ", ".join(f"{c.category} ({c.normalized_score:.0f})" for c in ranked[:2])
    strongest = ", ".join(
        f"{c.category} ({c.normalized_score:.0f})" for c in reversed(ranked[-2:])
    )
    plural = "s" if pages_analyzed != 1 else ""
    return (
        f"Estimated AI-search readiness {overall:.1f}/100 ({grade}) across "
        f"{pages_analyzed} page{plural}. Strongest: {strongest}. Weakest: "
        f"{weakest}. This is a heuristic over on-page signals, not a measurement "
        "or guarantee of how any AI system ranks or cites the site."
    )


def score_pages(
    pages: Sequence[WebsitePage],
    *,
    site_url: str,
    as_of: datetime | None = None,
) -> AiVisibilityResult:
    """Score a scan's pages into a full, reconciled :class:`AiVisibilityResult`.

    ``pages`` is the scan's crawled rows (failed fetches are filtered out here);
    ``site_url`` is the website's canonical URL, used only to read the scheme for
    the HTTPS check; ``as_of`` is the instant freshness is measured against and
    defaults to now (the service passes the scan's completion time, so a re-score
    of an old scan stays reproducible).

    Raises ``ValueError`` if there is nothing analysable to score -- the caller is
    expected to check first and simply not produce a report for a failed crawl,
    rather than have this invent a score for an empty site (spec section 46).
    """
    analysable = _analysable(pages)
    if not analysable:
        raise ValueError("cannot score a scan with no analysable pages")

    as_of = as_of or datetime.now(UTC)
    ctx = _build_context(analysable, site_url=site_url, as_of=as_of)

    by_category: dict[AiVisibilityCategory, list[Signal]] = {
        c: [] for c in AiVisibilityCategory
    }
    for signal in SIGNALS:
        by_category[signal.category].append(signal)

    categories = [
        _score_category(category, by_category[category], analysable, ctx)
        for category in AiVisibilityCategory
    ]

    overall = round(sum(c.normalized_score * c.weight for c in categories), _SCORE_DP)
    grade = ScoreGrade.from_score(overall).value
    summary = _summarise(overall, grade, len(analysable), categories)

    # Denormalised flags for fast list filtering. Each reads a distinct fact so no
    # flag simply mirrors a category score. has_clear_entity_info keys off the
    # representative page (homepage-first) plus site-level entity schema.
    lead = analysable[0]
    has_clear_entity_info = bool(
        (lead.title or "").strip()
        and (
            (lead.meta_description or "").strip()
            or (ctx.site_schema_types & _ORG_SCHEMA_TYPES)
        )
    )

    return AiVisibilityResult(
        overall_score=overall,
        grade=grade,
        points_earned=overall,
        points_available=100.0,
        pages_analyzed=len(analysable),
        engine_version=ENGINE_VERSION,
        summary=summary,
        has_faq_content=ctx.has_faq_anywhere,
        has_structured_data=any(p.has_schema_markup for p in analysable),
        has_clear_entity_info=has_clear_entity_info,
        has_freshness_signals=any(
            (p.published_date or p.modified_date) is not None for p in analysable
        ),
        categories=categories,
    )


__all__ = [
    "AiCategoryResult",
    "AiVisibilityResult",
    "score_pages",
]
