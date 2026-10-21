"""Response schemas for AI-visibility reports and their category scores (Module 7).

Read-only, exactly like the SEO schemas: a report is produced by scoring a scan,
never by posting a body, and nothing is massaged here -- the percentages a client
sees are the columns the engine stored, so the honesty verifier reconstructs the
same numbers from the same rows (spec section 24). There is no issue list: an
AI-visibility category is a set of signals present or absent, not a list of
mistakes.

Two tiers, matching the rest of the API: :class:`AiVisibilityReportOut` is the
headline a list or a poll needs, and :class:`AiVisibilityReportDetailOut` adds the
per-category breakdown for a single-report fetch. Both carry the ``disclaimer``
verbatim from the row, so the "this is a heuristic, not a ranking guarantee"
caveat (spec sections 7 and 45) travels with the number wherever it goes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AiVisibilityCategoryScoreOut(BaseModel):
    """One weighted signal group within a report.

    ``points_earned`` / ``points_available`` are the engine's own units,
    ``normalized_score`` is that as 0-100 so groups with different maximums stay
    comparable on a radar chart, and ``weight`` is the group's share of the
    headline. ``signal_results`` records every signal the group ran -- passed,
    failed, or not applicable -- so the score is auditable without the DB, and
    ``explanation`` says in words which signals drove it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: str
    points_earned: float
    points_available: float
    normalized_score: float
    weight: float
    explanation: str | None
    signal_results: dict[str, Any] = Field(default_factory=dict)


class AiVisibilityReportOut(BaseModel):
    """The headline AI-visibility result for a scan.

    ``overall_score`` is an *estimate of AI-search readiness*, never a guarantee of
    ranking or citation -- ``disclaimer`` carries that statement verbatim from the
    row, and the number is fully reconstructible from the category breakdown in the
    detail view. The four booleans are the at-a-glance signals a dashboard filters
    on without opening the full breakdown.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scan_id: uuid.UUID
    overall_score: float
    grade: str
    points_earned: float
    points_available: float
    pages_analyzed: int

    engine_version: str
    summary: str | None
    disclaimer: str

    has_faq_content: bool
    has_structured_data: bool
    has_clear_entity_info: bool
    has_freshness_signals: bool

    created_at: datetime
    updated_at: datetime


class AiVisibilityReportDetailOut(AiVisibilityReportOut):
    """A single report with its per-category breakdown.

    The route sorts ``categories`` by descending weight so the highest-impact
    groups lead; each carries the per-signal detail the radar chart and the
    explanation panel draw from.
    """

    categories: list[AiVisibilityCategoryScoreOut] = Field(default_factory=list)


__all__ = [
    "AiVisibilityCategoryScoreOut",
    "AiVisibilityReportDetailOut",
    "AiVisibilityReportOut",
]
