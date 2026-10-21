"""Response and request schemas for recommendations (Module 8).

A recommendation is *produced* by reconciling a scan's findings, never by posting
a body, so the output schema is the read projection of the ``recommendations``
row and the only input schema is the small status change a user can make. Nothing
is massaged here: ``priority_score``, ``estimated_score_gain`` and the impact /
effort / bucket a client sees are exactly the columns the engine stored, so the
same numbers reconstruct from the same rows (spec section 24).

``source`` is the one derived field -- ``"ai_visibility"`` for an AI-visibility
finding, ``"seo"`` otherwise -- computed from ``category`` rather than stored,
because it is a pure function of it (the engine files every AI finding under
``AI_VISIBILITY``). ``estimated_score_gain`` is always presented as an estimate,
never a promise (spec sections 7 and 45).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models.enums import RecommendationCategory, RecommendationStatus


class RecommendationOut(BaseModel):
    """One recommendation, the read projection of a ``recommendations`` row.

    ``estimated_score_gain`` is the share of the 0-100 headline this fix is
    estimated to recover; it reconstructs from ``evidence`` and the source report
    (spec section 24). ``is_open`` mirrors the model property (pending or in
    progress). ``resolved_by_scan_id`` is set only when a later scan *observed* the
    underlying issue gone -- a user marking it done by hand does not set it, so it
    stays an honest record of a verified fix.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    origin_scan_id: uuid.UUID | None
    resolved_by_scan_id: uuid.UUID | None

    rule_id: str
    title: str
    description: str
    category: str
    severity: str
    impact: str
    effort: str
    priority_score: float
    priority_bucket: str
    suggested_action: str
    estimated_score_gain: float | None

    status: str
    is_open: bool
    status_changed_at: datetime | None
    dismissed_reason: str | None

    affected_page_count: int
    example_urls: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    ai_explanation: str | None

    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def source(self) -> str:
        """Which engine raised it: ``"ai_visibility"`` or ``"seo"``.

        Derived from ``category`` -- the AI-visibility engine files every finding
        under ``AI_VISIBILITY``, so this is a pure function of the stored category,
        not a separate stored fact that could drift out of sync with it.
        """
        if self.category == RecommendationCategory.AI_VISIBILITY.value:
            return "ai_visibility"
        return "seo"


class RecommendationSummary(BaseModel):
    """Counts and the estimated headline points on the table for a business.

    ``estimated_total_gain`` sums ``estimated_score_gain`` over the *open*
    recommendations only -- a rough sense of how many headline points are
    recoverable, and, like every gain here, a sum of estimates rather than a
    promise. The breakdowns count *all* of a business's recommendations so
    completed and dismissed work stays visible, not just the open queue.
    """

    total: int
    open: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_bucket: dict[str, int] = Field(default_factory=dict)
    by_category: dict[str, int] = Field(default_factory=dict)
    estimated_total_gain: float


class RecommendationListOut(BaseModel):
    """A business's recommendations plus the summary the dashboard header shows."""

    items: list[RecommendationOut] = Field(default_factory=list)
    summary: RecommendationSummary


class RecommendationStatusUpdate(BaseModel):
    """The one change a user can make to a recommendation: its status.

    ``extra="forbid"`` so a typo'd field is a 422, not a silent no-op.
    ``dismissed_reason`` is accepted only alongside ``status="dismissed"`` -- a
    reason attached to any other transition is a client mistake and is rejected
    rather than quietly dropped.
    """

    model_config = ConfigDict(extra="forbid")

    status: RecommendationStatus
    dismissed_reason: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _reason_only_when_dismissed(self) -> RecommendationStatusUpdate:
        if self.status != RecommendationStatus.DISMISSED and self.dismissed_reason:
            raise ValueError(
                "dismissed_reason may only be set when status is 'dismissed'."
            )
        return self


__all__ = [
    "RecommendationListOut",
    "RecommendationOut",
    "RecommendationStatusUpdate",
    "RecommendationSummary",
]
