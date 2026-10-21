"""The SEO scoring engine: turn crawled pages into an explainable 0-100 score.

This is pure arithmetic over :mod:`app.engines.seo.rules`. It holds no database
session and calls no language model -- give it the pages a scan captured plus a
few whole-site facts and it returns a :class:`SeoScoreResult` that the service
layer persists verbatim. Keeping it side-effect-free is what lets the improvement
loop re-score an old scan and lets :mod:`scripts.verify_seo` prove the score
reconstructs from its parts without touching a server.

The reconciliation guarantee, which the verifier checks explicitly:

* within a category, ``sum(issue.points_deducted) == points_available - points_earned``;
* ``normalized_score == round(points_earned / points_available * 100, 1)``;
* ``overall_score == round(sum(category.normalized_score * category.weight), 1)``.

Every deduction is therefore traceable to the exact rule that produced it, with no
number appearing that the evidence cannot reproduce (spec section 24).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.engines.seo.rules import (
    CATEGORY_MAX_POINTS,
    CATEGORY_WEIGHTS,
    ENGINE_VERSION,
    RULES,
    Rule,
    SiteContext,
)
from app.models.enums import ScoreGrade, SeoCategory, Severity

if TYPE_CHECKING:
    from app.models.website import WebsitePage

# Round money-like point values to 4 decimals so the reconciliation identities are
# exact under float arithmetic; round the two display scores (category and
# overall) to 1 decimal, which is what the model stores and the UI shows.
_POINTS_DP = 4
_SCORE_DP = 1


@dataclass(frozen=True)
class IssueResult:
    """One detected problem, ready to become a ``SeoIssue`` row.

    ``anchor_normalized_url`` is how the service resolves ``page_id``: a page-scoped
    issue is anchored to a representative affected page (the homepage if it is one
    of them, else the first in crawl order), while a site-scoped issue anchors to
    no page (``None``) and reports the whole site as affected.
    """

    rule_id: str
    category: str
    severity: str
    title: str
    description: str
    suggested_fix: str
    points_deducted: float
    evidence: dict
    affected_page_count: int
    page_url: str | None = None
    anchor_normalized_url: str | None = None


@dataclass(frozen=True)
class CategoryResult:
    category: str
    points_earned: float
    points_available: float
    normalized_score: float
    weight: float
    issue_count: int
    check_results: dict


@dataclass(frozen=True)
class SeoScoreResult:
    overall_score: float
    grade: str
    points_earned: float
    points_available: float
    pages_analyzed: int
    issue_count: int
    critical_count: int
    high_count: int
    medium_count: int
    low_count: int
    engine_version: str
    summary: str
    categories: list[CategoryResult] = field(default_factory=list)
    issues: list[IssueResult] = field(default_factory=list)


def _analysable(pages: Sequence[WebsitePage]) -> list[WebsitePage]:
    """Pages that yielded HTML, in a deterministic order.

    Failed fetches are excluded from every average (they have no signals to
    score), and the sort -- homepage first, then by depth, then URL -- makes issue
    anchoring and therefore the whole result reproducible regardless of the order
    the rows arrive in.
    """
    fetched = [p for p in pages if p.was_fetched]
    return sorted(fetched, key=lambda p: (not p.is_homepage, p.depth, p.normalized_url))


def _build_context(
    pages: Sequence[WebsitePage],
    *,
    site_url: str,
    robots_found: bool,
    sitemap_found: bool,
    keywords: Sequence[str] | None,
) -> SiteContext:
    schema_types: set[str] = set()
    for page in pages:
        for value in page.schema_types or []:
            schema_types.add(str(value).lower())
    return SiteContext(
        is_https=urlsplit(site_url).scheme == "https",
        robots_found=robots_found,
        sitemap_found=sitemap_found,
        analysable_page_count=len(pages),
        keywords=tuple(keywords or ()),
        site_schema_types=frozenset(schema_types),
    )


def _pick_anchor(
    failing: list[tuple[WebsitePage, float, dict]],
) -> tuple[WebsitePage, dict]:
    """The page an issue points at: the homepage if it is affected, else the first."""
    for page, _frac, evidence in failing:
        if page.is_homepage:
            return page, evidence
    page, _frac, evidence = failing[0]
    return page, evidence


def _score_category(
    category: SeoCategory,
    rules: list[Rule],
    pages: list[WebsitePage],
    ctx: SiteContext,
) -> tuple[CategoryResult, list[IssueResult]]:
    """Score one category: evaluate its rules, roll failures into issues.

    Returns the category result and the issues it produced. ``points_earned`` is
    derived as ``points_available - sum(deductions)`` so it can never disagree with
    the issue list.
    """
    available = 0.0
    total_deduction = 0.0
    check_results: dict[str, dict] = {}
    issues: list[IssueResult] = []
    active_any = False

    for rule in rules:
        if rule.scope == "site":
            assert rule.evaluate_site is not None  # noqa: S101 - guaranteed by _validate_ruleset
            fraction, evidence = rule.evaluate_site(ctx)
            n_failing = 0 if fraction >= 1.0 else ctx.analysable_page_count
            anchor_url: str | None = None
            anchor_norm: str | None = None
            issue_evidence = dict(evidence)
        else:
            assert rule.evaluate is not None  # noqa: S101 - guaranteed by _validate_ruleset
            evaluated = [
                (page, *rule.evaluate(page, ctx))
                for page in pages
                if rule.applies is None or rule.applies(page, ctx)
            ]
            if not evaluated:
                # Not applicable to any page (e.g. an alt-text rule on a site with no
                # images). Record it as inactive and let it fall out of the totals.
                check_results[rule.rule_id] = {
                    "passed": True,
                    "points": 0.0,
                    "max": 0.0,
                    "applicable": False,
                }
                continue
            fractions = [f for _p, f, _e in evaluated]
            fraction = sum(fractions) / len(fractions)
            failing = [(p, f, e) for p, f, e in evaluated if f < 1.0]
            n_failing = len(failing)
            if failing:
                anchor_page, anchor_evidence = _pick_anchor(failing)
                anchor_url = anchor_page.url
                anchor_norm = anchor_page.normalized_url
                issue_evidence = dict(anchor_evidence)
                issue_evidence["pages_affected"] = n_failing
                issue_evidence["pages_checked"] = len(evaluated)
            else:
                anchor_url = anchor_norm = None
                issue_evidence = {}

        active_any = True
        available += rule.weight
        deduction = round(rule.weight * (1.0 - fraction), _POINTS_DP)
        total_deduction += deduction
        check_results[rule.rule_id] = {
            "passed": deduction == 0.0,
            "points": round(rule.weight - deduction, _POINTS_DP),
            "max": rule.weight,
            "applicable": True,
        }
        if deduction > 0.0:
            issues.append(
                IssueResult(
                    rule_id=rule.rule_id,
                    category=category.value,
                    severity=rule.severity.value,
                    title=rule.title,
                    description=rule.describe(issue_evidence),
                    suggested_fix=rule.fix,
                    points_deducted=deduction,
                    evidence=issue_evidence,
                    affected_page_count=n_failing,
                    page_url=anchor_url,
                    anchor_normalized_url=anchor_norm,
                )
            )

    if not active_any:
        # No rule in this category applied to anything. Award full marks against the
        # nominal maximum rather than dividing by zero or silently dropping the
        # category (which would re-weight the others). "Nothing to fix here."
        nominal = CATEGORY_MAX_POINTS[category]
        for rule in rules:
            check_results[rule.rule_id] = {
                "passed": True,
                "points": rule.weight,
                "max": rule.weight,
                "applicable": False,
            }
        result = CategoryResult(
            category=category.value,
            points_earned=nominal,
            points_available=nominal,
            normalized_score=100.0,
            weight=CATEGORY_WEIGHTS[category],
            issue_count=0,
            check_results=check_results,
        )
        return result, []

    earned = round(available - total_deduction, _POINTS_DP)
    normalized = round(earned / available * 100, _SCORE_DP)
    result = CategoryResult(
        category=category.value,
        points_earned=earned,
        points_available=available,
        normalized_score=normalized,
        weight=CATEGORY_WEIGHTS[category],
        issue_count=len(issues),
        check_results=check_results,
    )
    return result, issues


def _summarise(
    overall: float,
    grade: str,
    pages_analyzed: int,
    counts: dict[str, int],
    categories: list[CategoryResult],
) -> str:
    """A deterministic one-paragraph summary -- generated here, never by the AI."""
    weakest = sorted(categories, key=lambda c: c.normalized_score)[:2]
    weak = ", ".join(f"{c.category} ({c.normalized_score:.0f})" for c in weakest)
    total = counts["critical"] + counts["high"] + counts["medium"] + counts["low"]
    plural = "s" if pages_analyzed != 1 else ""
    return (
        f"Estimated SEO score {overall:.1f}/100 ({grade}) across {pages_analyzed} "
        f"page{plural}. {total} issue(s): {counts['critical']} critical, "
        f"{counts['high']} high, {counts['medium']} medium, {counts['low']} low. "
        f"Weakest areas: {weak}."
    )


def score_pages(
    pages: Sequence[WebsitePage],
    *,
    site_url: str,
    robots_found: bool,
    sitemap_found: bool,
    keywords: Sequence[str] | None = None,
) -> SeoScoreResult:
    """Score a scan's pages into a full, reconciled :class:`SeoScoreResult`.

    ``pages`` is the scan's crawled rows (failed fetches are filtered out here);
    ``site_url`` is the website's canonical URL, used only to read the scheme for
    the HTTPS check; ``robots_found`` / ``sitemap_found`` come from the scan's
    stored site analysis; ``keywords`` are the business's target terms, enabling
    the keyword-coverage rule when present.

    Raises ``ValueError`` if there is nothing analysable to score -- the caller is
    expected to check first and simply not produce a report for a failed crawl,
    rather than have this invent a 100/100 for an empty site.
    """
    analysable = _analysable(pages)
    if not analysable:
        raise ValueError("cannot score a scan with no analysable pages")

    ctx = _build_context(
        analysable,
        site_url=site_url,
        robots_found=robots_found,
        sitemap_found=sitemap_found,
        keywords=keywords,
    )

    by_category: dict[SeoCategory, list[Rule]] = {c: [] for c in SeoCategory}
    for rule in RULES:
        by_category[rule.category].append(rule)

    categories: list[CategoryResult] = []
    all_issues: list[IssueResult] = []
    for category in SeoCategory:
        result, issues = _score_category(
            category, by_category[category], analysable, ctx
        )
        categories.append(result)
        all_issues.extend(issues)

    overall = round(sum(c.normalized_score * c.weight for c in categories), _SCORE_DP)
    grade = ScoreGrade.from_score(overall).value

    counts = {
        Severity.CRITICAL.value: 0,
        Severity.HIGH.value: 0,
        Severity.MEDIUM.value: 0,
        Severity.LOW.value: 0,
    }
    for issue in all_issues:
        if issue.severity in counts:
            counts[issue.severity] += 1

    summary = _summarise(overall, grade, len(analysable), counts, categories)

    return SeoScoreResult(
        overall_score=overall,
        grade=grade,
        points_earned=overall,
        points_available=100.0,
        pages_analyzed=len(analysable),
        issue_count=len(all_issues),
        critical_count=counts[Severity.CRITICAL.value],
        high_count=counts[Severity.HIGH.value],
        medium_count=counts[Severity.MEDIUM.value],
        low_count=counts[Severity.LOW.value],
        engine_version=ENGINE_VERSION,
        summary=summary,
        categories=categories,
        issues=all_issues,
    )


__all__ = [
    "CategoryResult",
    "IssueResult",
    "SeoScoreResult",
    "score_pages",
]
