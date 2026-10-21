"""Improvement history for the continuous improvement loop (Module 20).

This table is what turns AURA from a repeated one-shot analyser into a feedback
loop. Each row records a transition between two scans of the same website: what
the scores were, what they became, and which recommendations were observed fixed
in between.

The attribution is evidence-based rather than assumed. A recommendation counts as
applied only when the specific issue (identified by ``rule_id``) present in the
earlier scan is absent from the later one. A score can therefore rise without any
recommendation being credited - which is the honest outcome when the user changed
something AURA never suggested.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDPrimaryKeyMixin


class ImprovementRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A measured before/after transition between two scans of one website."""

    __tablename__ = "improvement_records"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    website_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    previous_scan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_scans.id", ondelete="CASCADE"),
        nullable=False,
    )
    current_scan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_scans.id", ondelete="CASCADE"),
        nullable=False,
    )

    # --- score movement -----------------------------------------------------
    previous_seo_score: Mapped[float] = mapped_column(Float, nullable=False)
    current_seo_score: Mapped[float] = mapped_column(Float, nullable=False)
    seo_delta: Mapped[float] = mapped_column(Float, nullable=False)

    previous_ai_visibility_score: Mapped[float] = mapped_column(Float, nullable=False)
    current_ai_visibility_score: Mapped[float] = mapped_column(Float, nullable=False)
    ai_visibility_delta: Mapped[float] = mapped_column(Float, nullable=False)

    # --- attribution --------------------------------------------------------
    recommendations_applied: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Recommendation ids and rule ids confirmed fixed, with the per-rule score
    # contribution where it can be isolated.
    applied_details: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    # Issues present in the current scan but not the previous one, so a
    # regression is reported rather than netted away against improvements.
    new_issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    issues_resolved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues_introduced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Per-category movement, for the improvement breakdown chart.
    category_deltas: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    # False when the two scans were produced by different engine versions, in
    # which case the delta is not a like-for-like comparison and the UI says so
    # instead of reporting a misleading number.
    is_comparable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    incomparable_reason: Mapped[str | None] = mapped_column(String(200))

    summary: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "previous_scan_id", "current_scan_id", name="unique_scan_transition"
        ),
        Index("ix_improvement_business_created", "business_id", "created_at"),
        Index("ix_improvement_website_created", "website_id", "created_at"),
        CheckConstraint(
            "previous_scan_id <> current_scan_id", name="distinct_scan_transition"
        ),
    )
