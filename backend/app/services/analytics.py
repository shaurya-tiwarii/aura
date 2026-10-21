"""Normalise platform analytics and build honest daily dashboard snapshots."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationFailedError
from app.models.analytics import AnalyticsMetric, AnalyticsSnapshot
from app.models.business import Business
from app.models.enums import MetricKind, MetricSource, Platform
from app.models.publishing import Post
from app.schemas.analytics import AnalyticsMetricIn

_FIELDS: dict[str, dict[str, tuple[MetricKind, ...]]] = {
    Platform.LINKEDIN.value: {
        "impressionCount": (MetricKind.IMPRESSIONS,),
        "clickCount": (MetricKind.CLICKS,),
        "likeCount": (MetricKind.LIKES,),
        "commentCount": (MetricKind.COMMENTS,),
        "shareCount": (MetricKind.SHARES,),
    },
    Platform.REDDIT.value: {
        "score": (MetricKind.LIKES, MetricKind.ENGAGEMENT),
        "num_comments": (MetricKind.COMMENTS,),
        "views": (MetricKind.REACH, MetricKind.IMPRESSIONS),
    },
    Platform.WORDPRESS.value: {
        "views": (MetricKind.REACH, MetricKind.WEBSITE_SESSIONS),
        "impressions": (MetricKind.IMPRESSIONS,),
        "likes": (MetricKind.LIKES,),
        "comments": (MetricKind.COMMENTS,),
        "shares": (MetricKind.SHARES,),
        "clicks": (MetricKind.CLICKS,),
    },
}


def _post_for_business(
    session: Session, business: Business, post_id: uuid.UUID
) -> Post:
    post = session.scalar(
        select(Post)
        .join(Post.content)
        .where(Post.id == post_id, Post.content.has(business_id=business.id))
    )
    if post is None:
        raise NotFoundError(
            "That published post could not be found.",
            hint="Analytics can only be attached to posts owned by this business.",
        )
    return post


def _normalise_fields(
    platform: str, payload: dict[str, Any]
) -> dict[str, tuple[float, str]]:
    result: dict[str, tuple[float, str]] = {}
    for field, raw in payload.items():
        if field not in _FIELDS.get(platform, {}):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationFailedError(
                f"The analytics field '{field}' is not numeric."
            ) from exc
        if value < 0:
            raise ValidationFailedError("Analytics values must not be negative.")
        for kind in _FIELDS[platform][field]:
            # A platform field can legitimately map to more than one common
            # vocabulary item (for example Reddit score).
            result.setdefault(kind.value, (value, field))
    return result


def ingest(
    session: Session, business: Business, payload: AnalyticsMetricIn
) -> list[AnalyticsMetric]:
    post = _post_for_business(session, business, payload.post_id)
    if post.platform != payload.platform.value:
        raise ValidationFailedError("The platform does not match the published post.")
    if payload.source == MetricSource.DERIVED:
        raise ValidationFailedError(
            "Derived metrics are calculated by AURA and cannot be submitted.",
            hint="Submit the underlying platform_api, demo, or user_entered metrics.",
        )
    if payload.source == MetricSource.PLATFORM_API and (
        post.mode != "live" or post.connection is None
    ):
        raise ValidationFailedError(
            "Live platform metrics require a live platform connection.",
            hint="Use source='demo' for a demo connection; AURA never invents live data.",
        )
    if payload.source == MetricSource.DEMO and post.mode != "demo":
        raise ValidationFailedError(
            "Demo metrics can only be recorded for a demo post.",
            hint="Use source='platform_api' only for a real API response.",
        )

    values: dict[str, tuple[float, str]] = {
        kind.value: (float(value), kind.value)
        for kind, value in payload.metrics.items()
    }
    for kind, item in _normalise_fields(
        payload.platform.value, payload.raw_payload
    ).items():
        values.setdefault(kind, item)
    if not values:
        raise ValidationFailedError(
            "No recognised analytics metrics were supplied.",
            hint="Provide metrics or a supported platform field in raw_payload.",
        )

    now = datetime.now(UTC)
    result: list[AnalyticsMetric] = []
    for kind, (value, source_field) in values.items():
        existing = session.scalar(
            select(AnalyticsMetric).where(
                AnalyticsMetric.post_id == post.id,
                AnalyticsMetric.metric_kind == kind,
                AnalyticsMetric.measured_on == payload.measured_on,
                AnalyticsMetric.source == payload.source.value,
            )
        )
        if existing is None:
            existing = AnalyticsMetric(
                post_id=post.id,
                platform=post.platform,
                metric_kind=kind,
                value=value,
                source=payload.source.value,
                source_field=source_field,
                measured_on=payload.measured_on,
                collected_at=now,
                raw_payload=payload.raw_payload,
            )
            session.add(existing)
        else:
            existing.value = value
            existing.source_field = source_field
            existing.collected_at = now
            existing.raw_payload = payload.raw_payload
        result.append(existing)
    session.flush()
    return result


def list_metrics(
    session: Session,
    business: Business,
    *,
    start: date | None = None,
    end: date | None = None,
    source: str | None = None,
) -> list[AnalyticsMetric]:
    conditions = [Post.content.has(business_id=business.id)]
    if start:
        conditions.append(AnalyticsMetric.measured_on >= start)
    if end:
        conditions.append(AnalyticsMetric.measured_on <= end)
    if source:
        conditions.append(AnalyticsMetric.source == source)
    return list(
        session.scalars(
            select(AnalyticsMetric)
            .join(AnalyticsMetric.post)
            .where(*conditions)
            .order_by(AnalyticsMetric.measured_on.desc(), AnalyticsMetric.metric_kind)
        )
    )


def rebuild_snapshots(
    session: Session,
    business: Business,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[AnalyticsSnapshot]:
    metrics = list_metrics(session, business, start=start, end=end)
    if not metrics:
        return []
    dates = sorted({metric.measured_on for metric in metrics})
    for day in dates:
        session.execute(
            delete(AnalyticsSnapshot).where(
                AnalyticsSnapshot.business_id == business.id,
                AnalyticsSnapshot.snapshot_date == day,
            )
        )
    posts = list(
        session.scalars(select(Post).where(Post.content.has(business_id=business.id)))
    )
    by_key: dict[tuple[date, str | None], list[AnalyticsMetric]] = {}
    for metric in metrics:
        by_key.setdefault((metric.measured_on, metric.platform), []).append(metric)
        by_key.setdefault((metric.measured_on, None), []).append(metric)
    snapshots: list[AnalyticsSnapshot] = []
    fields = {
        MetricKind.REACH.value: "total_reach",
        MetricKind.IMPRESSIONS.value: "total_impressions",
        MetricKind.ENGAGEMENT.value: "total_engagement",
        MetricKind.LIKES.value: "total_likes",
        MetricKind.COMMENTS.value: "total_comments",
        MetricKind.SHARES.value: "total_shares",
        MetricKind.CLICKS.value: "total_clicks",
    }
    for (day, platform), rows in by_key.items():
        totals = {field: 0.0 for field in fields.values()}
        breakdown: dict[str, float] = {}
        for row in rows:
            if row.metric_kind in fields:
                totals[fields[row.metric_kind]] += row.value
            breakdown[row.source] = breakdown.get(row.source, 0.0) + row.value
        impressions = totals["total_impressions"]
        published = sum(
            1
            for post in posts
            if (platform is None or post.platform == platform)
            and post.published_at is not None
            and post.published_at.date() == day
        )
        snapshot = AnalyticsSnapshot(
            business_id=business.id,
            snapshot_date=day,
            platform=platform,
            **totals,
            posts_published=published,
            engagement_rate=(
                round(totals["total_engagement"] / impressions, 4)
                if impressions
                else None
            ),
            has_real_data=MetricSource.PLATFORM_API.value in breakdown,
            has_demo_data=MetricSource.DEMO.value in breakdown,
            source_breakdown=breakdown,
        )
        session.add(snapshot)
        snapshots.append(snapshot)
    session.flush()
    return snapshots


def list_snapshots(
    session: Session,
    business: Business,
    *,
    start: date | None = None,
    end: date | None = None,
) -> list[AnalyticsSnapshot]:
    conditions = [AnalyticsSnapshot.business_id == business.id]
    if start:
        conditions.append(AnalyticsSnapshot.snapshot_date >= start)
    if end:
        conditions.append(AnalyticsSnapshot.snapshot_date <= end)
    return list(
        session.scalars(
            select(AnalyticsSnapshot)
            .where(*conditions)
            .order_by(
                AnalyticsSnapshot.snapshot_date.desc(), AnalyticsSnapshot.platform
            )
        )
    )
