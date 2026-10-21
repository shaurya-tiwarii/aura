"""Response schemas for SEO reports, category scores, and issues (Module 6).

Read-only, like the scan schemas: a report is produced by scoring a scan, never
by posting a body. The shape mirrors the three tables it projects
(:mod:`app.models.seo`) and nothing is massaged in this layer -- the percentages
a client sees are the columns the engine stored, so the honesty verifier
reconstructs the same numbers from the same rows (spec section 24).

Two tiers, matching the rest of the API: :class:`SeoReportOut` is the headline a
list or a poll needs, and :class:`SeoReportDetailOut` adds the per-category
breakdown and the full issue list for a single-report fetch.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SeoIssueOut(BaseModel):
    """One detected problem, traceable to the rule that produced it.

    ``rule_id`` is the stable check identifier (e.g. ``metadata.title_missing``)
    and ``points_deducted`` is exactly what this issue removed from its category,
    so summing a category's issues reconstructs its score. ``evidence`` is the
    machine-readable "why" -- the values that decided the deduction.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_id: uuid.UUID | None
    rule_id: str
    category: str
    severity: str
    title: str
    description: str
    suggested_fix: str | None
    points_deducted: float
    evidence: dict[str, Any] = Field(default_factory=dict)
    page_url: str | None
    affected_page_count: int


class SeoCategoryScoreOut(BaseModel):
    """One weighted category within a report.

    ``points_earned`` / ``points_available`` are the engine's own units ("9 of 10"),
    ``normalized_score`` is that as 0-100 so categories with different maximums stay
    comparable on a radar chart, and ``weight`` is the category's share of the
    headline. ``check_results`` records every rule the category ran, including the
    ones that passed or did not apply, so the score is auditable without the DB.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: str
    points_earned: float
    points_available: float
    normalized_score: float
    weight: float
    issue_count: int
    check_results: dict[str, Any] = Field(default_factory=dict)


class SeoReportOut(BaseModel):
    """The headline SEO result for a scan.

    ``overall_score`` is an *estimate of AI-search readiness*, never a guarantee of
    ranking -- the disclaimer is carried by ``engine_version`` and the summary, and
    the number is fully reconstructible from the issues in the detail view. The
    severity counts sit alongside the total so "62/100" is read next to what is
    wrong, not in isolation.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scan_id: uuid.UUID
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
    summary: str | None

    created_at: datetime
    updated_at: datetime


class SeoReportDetailOut(SeoReportOut):
    """A single report with its category breakdown and full issue list.

    The route sorts ``issues`` most-severe-first (then by points lost) so the
    highest-impact fixes are at the top; ``categories`` carries the per-category
    scores the radar chart draws.
    """

    categories: list[SeoCategoryScoreOut] = Field(default_factory=list)
    issues: list[SeoIssueOut] = Field(default_factory=list)


__all__ = [
    "SeoCategoryScoreOut",
    "SeoIssueOut",
    "SeoReportDetailOut",
    "SeoReportOut",
]
