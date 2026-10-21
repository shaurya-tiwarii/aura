"""The recommendation ranking engine: findings -> prioritised action drafts.

Pure and deterministic. It consumes normalised findings -- :class:`SeoIssueInput`
(one per fired SEO rule) and :class:`AiSignalGapInput` (one per missing/partial
AI-visibility signal) -- and returns ordered :class:`RecommendationDraft` objects
the service persists. It holds no session and imports no language model; the
service reads the stored SEO and AI reports and normalises them into the inputs,
so this module never touches SQLAlchemy or the ORM.

Every number a draft carries reconstructs from its input (spec section 24):

* ``estimated_score_gain`` = ``points_lost / category_points_available * 100 *
  category_weight`` -- the share of the 0-100 headline this fix would recover.
  Because each engine's category weights sum to 1.0 and, within a category, the
  lost points sum to ``available - earned``, the gains of all of one source's
  drafts sum to ``100 - that source's overall score``. That identity is the proof
  the list is neither inventing nor dropping impact, and the verifier checks it.
* ``impact`` buckets that gain (>= 4 high, >= 1.5 medium, else low);
* ``effort`` comes from the catalog;
* ``priority_score`` = ``impact.weight / effort.weight`` -- high value, low cost
  first; and ``priority_bucket`` is the impact x effort 2x2 the UI groups by.

The result is sorted by priority, then estimated gain, then severity, then id, so
two runs over the same findings produce byte-identical output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.engines.recommendation.catalog import (
    AI_REMEDIATION,
    EFFORT_BY_RULE,
    IMPACT_HIGH_MIN,
    IMPACT_MEDIUM_MIN,
    SEO_CATEGORY_MAP,
)
from app.models.enums import (
    EffortLevel,
    ImpactLevel,
    PriorityBucket,
    RecommendationCategory,
    Severity,
)

_GAIN_DP = 2
_PRIORITY_DP = 4


@dataclass(frozen=True)
class SeoIssueInput:
    """One fired SEO rule, normalised from a stored ``SeoIssue`` + its category.

    ``category_points_available`` and ``category_weight`` come from the issue's
    ``SeoCategoryScore`` row; together with ``points_deducted`` they are all the
    engine needs to compute the estimated score gain -- no re-scoring.
    """

    rule_id: str
    category: str  # SeoCategory value
    severity: str  # Severity value, taken from the rule
    title: str
    description: str
    suggested_fix: str | None
    points_deducted: float
    category_points_available: float
    category_weight: float
    affected_page_count: int = 0
    example_urls: tuple[str, ...] = ()
    evidence: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class AiSignalGapInput:
    """One missing or partial AI-visibility signal, normalised from signal_results.

    A *gap* is an applicable signal a site did not fully earn (``points < max``).
    The lost points are ``max_points - points``; with the category's available
    points and weight that yields the estimated gain, mirroring the SEO path.
    """

    signal_id: str
    category: str  # AiVisibilityCategory value (for evidence only)
    points: float
    max_points: float
    category_points_available: float
    category_weight: float
    detail: str = ""
    fraction: float = 0.0
    affected_page_count: int = 0
    evidence: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class RecommendationDraft:
    """A ranked, deduplicated action, ready to become a ``Recommendation`` row.

    ``rule_id`` is the raw dotted id of the SEO rule or AI signal that produced it
    -- the dedup key across scans and the anchor the improvement loop credits a fix
    against. ``source`` is ``"seo"`` or ``"ai_visibility"``; the service also
    derives it from ``category`` (AI findings are all ``AI_VISIBILITY``), but it is
    carried explicitly so the wiring is obvious.
    """

    source: str
    rule_id: str
    category: str  # RecommendationCategory value
    severity: str
    impact: str
    effort: str
    priority_score: float
    priority_bucket: str
    title: str
    description: str
    suggested_action: str
    estimated_score_gain: float
    affected_page_count: int
    example_urls: list[str]
    evidence: dict


def _score_gain(points_lost: float, available: float, weight: float) -> float:
    """Headline points this fix would recover: its share of its weighted category."""
    if available <= 0:
        return 0.0
    return round(points_lost / available * 100.0 * weight, _GAIN_DP)


def _impact_from_gain(gain: float) -> ImpactLevel:
    if gain >= IMPACT_HIGH_MIN:
        return ImpactLevel.HIGH
    if gain >= IMPACT_MEDIUM_MIN:
        return ImpactLevel.MEDIUM
    return ImpactLevel.LOW


# AI signals have no severity of their own, so it is derived from the (uniformly
# score-driven) impact. Kept 1:1 so a high-impact gap reads as high severity.
_SEVERITY_FROM_IMPACT: dict[ImpactLevel, Severity] = {
    ImpactLevel.HIGH: Severity.HIGH,
    ImpactLevel.MEDIUM: Severity.MEDIUM,
    ImpactLevel.LOW: Severity.LOW,
}

_LOW_EFFORT = frozenset({EffortLevel.TRIVIAL, EffortLevel.LOW})
_HIGH_IMPACT = frozenset({ImpactLevel.HIGH, ImpactLevel.MEDIUM})


def _priority(impact: ImpactLevel, effort: EffortLevel) -> float:
    """Value per unit of cost. Higher sorts first."""
    return round(impact.weight / effort.weight, _PRIORITY_DP)


def _bucket(impact: ImpactLevel, effort: EffortLevel) -> PriorityBucket:
    """The impact x effort 2x2 the UI groups by (spec section 21)."""
    high_impact = impact in _HIGH_IMPACT
    low_effort = effort in _LOW_EFFORT
    if high_impact and low_effort:
        return PriorityBucket.QUICK_WIN
    if high_impact:
        return PriorityBucket.MAJOR_PROJECT
    if low_effort:
        return PriorityBucket.FILL_IN
    return PriorityBucket.LONG_TERM


def _from_seo(issue: SeoIssueInput) -> RecommendationDraft:
    gain = _score_gain(
        issue.points_deducted, issue.category_points_available, issue.category_weight
    )
    impact = _impact_from_gain(gain)
    effort = EFFORT_BY_RULE[issue.rule_id]  # presence guaranteed by catalog._validate
    return RecommendationDraft(
        source="seo",
        rule_id=issue.rule_id,
        category=SEO_CATEGORY_MAP[issue.category].value,
        severity=issue.severity,
        impact=impact.value,
        effort=effort.value,
        priority_score=_priority(impact, effort),
        priority_bucket=_bucket(impact, effort).value,
        title=issue.title,
        description=issue.description,
        suggested_action=(issue.suggested_fix or "").strip()
        or "Review the affected pages and resolve this issue.",
        estimated_score_gain=gain,
        affected_page_count=issue.affected_page_count,
        example_urls=list(issue.example_urls),
        evidence=dict(issue.evidence),
    )


def _from_ai(gap: AiSignalGapInput) -> RecommendationDraft:
    remediation, effort = AI_REMEDIATION[gap.signal_id]  # guaranteed by _validate
    gain = _score_gain(
        gap.max_points - gap.points,
        gap.category_points_available,
        gap.category_weight,
    )
    impact = _impact_from_gain(gain)
    severity = _SEVERITY_FROM_IMPACT[impact]
    evidence = dict(gap.evidence)
    evidence.update(
        {
            "signal_id": gap.signal_id,
            "signal_category": gap.category,
            "detail": gap.detail,
            "fraction": gap.fraction,
            "points_earned": gap.points,
            "points_possible": gap.max_points,
        }
    )
    return RecommendationDraft(
        source="ai_visibility",
        rule_id=gap.signal_id,
        category=RecommendationCategory.AI_VISIBILITY.value,
        severity=severity.value,
        impact=impact.value,
        effort=effort.value,
        priority_score=_priority(impact, effort),
        priority_bucket=_bucket(impact, effort).value,
        title=remediation.title,
        description=remediation.description,
        suggested_action=remediation.action,
        estimated_score_gain=gain,
        affected_page_count=gap.affected_page_count,
        example_urls=[],
        evidence=evidence,
    )


def _ordering_key(draft: RecommendationDraft) -> tuple:
    """Priority desc, then estimated gain desc, then severity desc, then id asc.

    Every tiebreaker is total and deterministic, so the order is stable across
    runs and the id break makes it fully reproducible.
    """
    return (
        -draft.priority_score,
        -draft.estimated_score_gain,
        -Severity(draft.severity).rank,
        draft.rule_id,
    )


def build_recommendations(
    seo_inputs: Sequence[SeoIssueInput],
    ai_inputs: Sequence[AiSignalGapInput],
) -> list[RecommendationDraft]:
    """Fold SEO issues and AI-visibility gaps into one prioritised action list.

    The two id spaces are disjoint (enforced by :func:`catalog._validate`), so the
    combined list needs no cross-source dedup here; the service dedups against the
    *business's* existing recommendations by ``rule_id`` when it persists.
    """
    drafts = [_from_seo(issue) for issue in seo_inputs]
    drafts.extend(_from_ai(gap) for gap in ai_inputs)
    drafts.sort(key=_ordering_key)
    return drafts


__all__ = [
    "AiSignalGapInput",
    "RecommendationDraft",
    "SeoIssueInput",
    "build_recommendations",
]
