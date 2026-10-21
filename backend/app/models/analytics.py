"""Analytics metrics and dashboard snapshots (Modules 16-17).

Normalisation is the engineering contribution here (spec section 36, item 9).
Platforms disagree on what they count and what they call it: LinkedIn reports
``impressionCount``, Reddit reports ``score`` and ``upvote_ratio``, WordPress
reports view counts per post. Storing each platform's raw shape would push that
mess into every chart. Instead each connector maps its response onto the
``MetricKind`` vocabulary, keeps the untouched original in ``raw_payload`` for
auditability, and records where the number came from in ``source``.

``source`` is mandatory and is rendered as a badge everywhere a metric appears,
so a demo number is always visibly a demo number (spec section 25).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
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
from app.models.enums import MetricKind, MetricSource, Platform

if TYPE_CHECKING:
    from app.models.publishing import Post


class AnalyticsMetric(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One normalised measurement, at one point in time, for one post.

    Rows are append-only observations rather than a mutable "current value", so
    engagement over time is a real time series and a platform correcting its own
    numbers does not erase history.
    """

    __tablename__ = "analytics"

    post_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    platform: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    metric_kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Provenance. Never defaulted silently - the writer must state it.
    source: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # The platform's own field name, kept so a normalisation decision can be
    # audited later ("reach came from LinkedIn's impressionCount").
    source_field: Mapped[str | None] = mapped_column(String(80))

    # The day the metric describes, distinct from when it was collected.
    measured_on: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    raw_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    post: Mapped[Post] = relationship(back_populates="metrics")

    __table_args__ = (
        # One value per post/metric/day/source. Re-syncing the same day updates
        # in place instead of double-counting.
        UniqueConstraint(
            "post_id",
            "metric_kind",
            "measured_on",
            "source",
            name="unique_metric_per_post_day",
        ),
        Index("ix_analytics_post_kind", "post_id", "metric_kind"),
        Index("ix_analytics_platform_date", "platform", "measured_on"),
        CheckConstraint(
            f"metric_kind IN {MetricKind.sql_in_list()}", name="valid_metric_kind"
        ),
        CheckConstraint(
            f"source IN {MetricSource.sql_in_list()}", name="valid_metric_source"
        ),
        CheckConstraint(
            f"platform IN {Platform.sql_in_list()}", name="valid_metric_platform"
        ),
        CheckConstraint("value >= 0", name="non_negative_metric_value"),
    )

    @property
    def is_real(self) -> bool:
        """True only for measurements that came from a real platform API."""
        return self.source == MetricSource.PLATFORM_API.value


class AnalyticsSnapshot(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Pre-aggregated daily rollup per business, for the dashboard.

    The dashboard needs six trend charts on first paint. Recomputing them from
    raw metrics on every load would mean several grouped scans per request; a
    daily snapshot makes it one indexed range query.

    ``has_real_data`` / ``has_demo_data`` are carried up from the underlying
    metrics so an aggregate is never presented as fully real when part of it was
    simulated. Aggregation is exactly where that distinction would otherwise get
    lost.
    """

    __tablename__ = "analytics_snapshots"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Null means "all platforms combined" for this day.
    platform: Mapped[str | None] = mapped_column(String(20), index=True)

    total_reach: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_impressions: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_engagement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_likes: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_comments: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_shares: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_clicks: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    posts_published: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # engagement / impressions, computed once. Null when impressions are zero
    # rather than defaulting to 0, so "no data" is distinguishable from "0%".
    engagement_rate: Mapped[float | None] = mapped_column(Float)

    # Provenance rolled up from the contributing metrics.
    has_real_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_demo_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Per-source totals, so the UI can say "1,240 real / 3,000 simulated".
    source_breakdown: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "snapshot_date",
            "platform",
            name="unique_snapshot_per_day_platform",
        ),
        Index("ix_snapshots_business_date", "business_id", "snapshot_date"),
    )
