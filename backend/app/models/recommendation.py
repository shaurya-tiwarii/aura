"""Recommendations and content opportunities (Modules 8, 9, 21)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    JSONColumn,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import (
    ContentOpportunityStatus,
    EffortLevel,
    ImpactLevel,
    PriorityBucket,
    RecommendationStatus,
    Severity,
)

if TYPE_CHECKING:
    from app.models.business import Business
    from app.models.content import Content


class Recommendation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A prioritised, actionable improvement produced by the rule engine.

    Recommendations are owned by the *business*, not by a scan, because they
    outlive the scan that created them: a recommendation raised in scan 2 and
    still unfixed in scan 5 must keep its status and its history rather than
    being duplicated each run. ``rule_id`` plus business identity is the dedup
    key the engine uses to decide "already raised" versus "new".

    ``resolved_by_scan_id`` records the scan in which the underlying issue
    disappeared. That is what lets the improvement loop attribute a score delta
    to specific applied fixes (Module 20) instead of guessing.
    """

    __tablename__ = "recommendations"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The scan that first raised it.
    origin_scan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_scans.id", ondelete="SET NULL"),
        index=True,
    )
    # The scan in which it was observed fixed.
    resolved_by_scan_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_scans.id", ondelete="SET NULL"),
    )

    rule_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)

    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    impact: Mapped[str] = mapped_column(String(16), nullable=False)
    effort: Mapped[str] = mapped_column(String(16), nullable=False)
    # impact.weight / effort.weight, scaled. Computed once at write time so the
    # recommendation list can be ordered in SQL.
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Quick win vs long-term grouping shown in the UI.
    priority_bucket: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Concrete next step, in plain language for a non-technical owner.
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    # Estimated score gain if applied, from the rule's own deduction weight.
    # Presented as an estimate, never as a promise.
    estimated_score_gain: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=RecommendationStatus.PENDING.value,
        index=True,
    )
    status_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    dismissed_reason: Mapped[str | None] = mapped_column(String(300))

    affected_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Sample URLs, capped, so the user knows where to start.
    example_urls: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    # Supporting data copied from the issue that raised it.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Optional AI-written elaboration, generated on demand when the user asks
    # "explain this". Cached here to avoid re-billing the same question.
    ai_explanation: Mapped[str | None] = mapped_column(Text)

    business: Mapped[Business] = relationship(back_populates="recommendations")

    __table_args__ = (
        Index("ix_recommendations_business_status", "business_id", "status"),
        Index(
            "ix_recommendations_business_priority",
            "business_id",
            "status",
            "priority_score",
        ),
        # Reconciliation may be requested manually while a scan worker is also
        # finishing. The application treats (business_id, rule_id) as its dedup
        # key; this constraint makes that invariant hold under concurrent writes,
        # rather than merely relying on both transactions observing the same row.
        UniqueConstraint(
            "business_id",
            "rule_id",
            name="uq_recommendations_business_rule",
        ),
        CheckConstraint(
            f"status IN {RecommendationStatus.sql_in_list()}",
            name="valid_recommendation_status",
        ),
        CheckConstraint(
            f"severity IN {Severity.sql_in_list()}", name="valid_rec_severity"
        ),
        CheckConstraint(
            f"impact IN {ImpactLevel.sql_in_list()}", name="valid_rec_impact"
        ),
        CheckConstraint(
            f"effort IN {EffortLevel.sql_in_list()}", name="valid_rec_effort"
        ),
        CheckConstraint(
            f"priority_bucket IN {PriorityBucket.sql_in_list()}",
            name="valid_priority_bucket",
        ),
    )

    @property
    def is_open(self) -> bool:
        return self.status in {
            RecommendationStatus.PENDING.value,
            RecommendationStatus.IN_PROGRESS.value,
        }


class ContentOpportunity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A suggested piece of content to create (Module 9).

    Distinct from a ``Recommendation``: a recommendation says "fix what exists",
    an opportunity says "create something that does not exist yet". Accepting one
    creates a ``Content`` draft, and ``content_id`` keeps that link so the UI can
    show which ideas were acted on.

    ``rationale`` records *why* the opportunity was identified (a missing FAQ, a
    a service with no page, a topic with no coverage yet). That
    derivation is application logic, not an AI guess, and the user can see it.
    """

    __tablename__ = "content_opportunities"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("content.id", ondelete="SET NULL"),
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    suggested_content_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the idea came from: "missing_faq", "service_without_page",
    # "thin_content", "keyword_target".
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    target_keywords: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    suggested_outline: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    impact: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ImpactLevel.MEDIUM.value
    )
    effort: Mapped[str] = mapped_column(
        String(16), nullable=False, default=EffortLevel.MEDIUM.value
    )
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ContentOpportunityStatus.SUGGESTED.value,
        index=True,
    )

    business: Mapped[Business] = relationship(back_populates="opportunities")
    content: Mapped[Content | None] = relationship(back_populates="opportunity")

    __table_args__ = (
        Index("ix_opportunities_business_status", "business_id", "status"),
        CheckConstraint(
            f"status IN {ContentOpportunityStatus.sql_in_list()}",
            name="valid_opportunity_status",
        ),
    )
