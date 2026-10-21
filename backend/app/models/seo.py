"""SEO report, per-category scores, and individual issues (Module 6).

Three tables rather than one JSON blob, deliberately:

* ``seo_reports``  - one row per scan: the headline 0-100 score.
* ``seo_scores``   - one row per category per report. Separate rows make
  "show me the metadata score over the last 6 scans" a simple indexed query,
  which is what the trend charts need.
* ``seo_issues``   - one row per detected problem, each traceable to the page it
  was found on and the scoring rule that fired.

Every deduction is recorded as an issue, so the headline score is fully
reconstructible from its parts. That is the explainability requirement in spec
section 24: no number appears that cannot be traced to the rule that produced it.
"""

from __future__ import annotations

import uuid
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

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import ScoreGrade, Severity

if TYPE_CHECKING:
    from app.models.website import WebsiteScan


class SeoReport(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The headline SEO result for one scan."""

    __tablename__ = "seo_reports"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_scans.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Normalised 0-100, rounded to one decimal.
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[str] = mapped_column(String(16), nullable=False)

    # Raw weighted points earned / available, before normalisation. Kept so the
    # UI can show "43.5 of 55 points" alongside the percentage, and so a scoring
    # bug is diagnosable after the fact.
    points_earned: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    points_available: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    pages_analyzed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    medium_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Version of the scoring ruleset that produced this report. Without it,
    # comparing scores across a weight change would be meaningless - the
    # improvement loop checks this before reporting a delta.
    engine_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1.0"
    )
    # Human-readable summary of the biggest drivers, generated deterministically
    # from the issue set (not by the AI).
    summary: Mapped[str | None] = mapped_column(Text)

    scan: Mapped[WebsiteScan] = relationship(back_populates="seo_report")
    category_scores: Mapped[list[SeoCategoryScore]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    issues: Mapped[list[SeoIssue]] = relationship(
        back_populates="report",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "overall_score >= 0 AND overall_score <= 100", name="valid_overall_score"
        ),
    )

    @property
    def score_grade(self) -> ScoreGrade:
        return ScoreGrade.from_score(self.overall_score)


class SeoCategoryScore(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One weighted category within a report, e.g. ``metadata: 9/10``."""

    __tablename__ = "seo_scores"

    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("seo_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    category: Mapped[str] = mapped_column(String(30), nullable=False)
    # Points earned and the category's maximum, in the engine's own units. The
    # UI shows these directly ("Metadata: 9/10").
    points_earned: Mapped[float] = mapped_column(Float, nullable=False)
    points_available: Mapped[float] = mapped_column(Float, nullable=False)
    # Normalised 0-100 for this category alone, so categories with different
    # maximums remain comparable on a radar chart.
    normalized_score: Mapped[float] = mapped_column(Float, nullable=False)
    # This category's share of the overall score (the weight from the ruleset).
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Per-check outcomes within the category, e.g.
    # {"title_present": {"passed": true, "points": 3.0, "max": 3.0}, ...}
    check_results: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    report: Mapped[SeoReport] = relationship(back_populates="category_scores")

    __table_args__ = (
        UniqueConstraint("report_id", "category", name="unique_category_per_report"),
        CheckConstraint(
            "normalized_score >= 0 AND normalized_score <= 100",
            name="valid_category_score",
        ),
        CheckConstraint("points_available > 0", name="positive_points_available"),
    )


class SeoIssue(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single detected SEO problem.

    ``rule_id`` is the stable identifier of the check that fired (for example
    ``metadata.title_missing``). It is what links an issue to its recommendation
    and lets the improvement loop tell "this specific problem was fixed" from
    "the score happened to move".
    """

    __tablename__ = "seo_issues"

    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("seo_reports.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Null for site-wide issues (missing sitemap, no robots.txt).
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_pages.id", ondelete="CASCADE"),
        index=True,
    )

    rule_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # What the user should do about it, in plain language.
    suggested_fix: Mapped[str | None] = mapped_column(Text)

    # Points lost to this issue. Summing these per category reproduces the
    # category score exactly.
    points_deducted: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # The value that triggered the rule, for display: {"found": 0, "expected": 1}
    # or {"length": 12, "recommended_min": 30}.
    evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Which page URL this was found on, denormalised so the issue list renders
    # without joining back through pages.
    page_url: Mapped[str | None] = mapped_column(String(2048))
    # How many pages share this issue, when it has been rolled up.
    affected_page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    report: Mapped[SeoReport] = relationship(back_populates="issues")

    __table_args__ = (
        Index("ix_seo_issues_report_severity", "report_id", "severity"),
        Index("ix_seo_issues_report_category", "report_id", "category"),
        CheckConstraint(f"severity IN {Severity.sql_in_list()}", name="valid_severity"),
        CheckConstraint("points_deducted >= 0", name="non_negative_deduction"),
    )
