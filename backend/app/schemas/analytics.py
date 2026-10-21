"""Tenant-scoped analytics ingestion and dashboard projections."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import MetricKind, MetricSource, Platform


class AnalyticsMetricIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_id: uuid.UUID
    platform: Platform
    measured_on: date
    source: MetricSource
    # Raw platform response. The service maps platform-specific field names to the
    # MetricKind vocabulary; callers never need to know the storage shape.
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[MetricKind, float] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def non_negative(cls, value: dict[MetricKind, float]) -> dict[MetricKind, float]:
        if any(number < 0 for number in value.values()):
            raise ValueError("Metric values must not be negative.")
        return value


class AnalyticsMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    platform: str
    metric_kind: str
    value: float
    source: str
    source_field: str | None
    measured_on: date
    collected_at: datetime
    raw_payload: dict[str, Any]


class AnalyticsSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    snapshot_date: date
    platform: str | None
    total_reach: float
    total_impressions: float
    total_engagement: float
    total_likes: float
    total_comments: float
    total_shares: float
    total_clicks: float
    posts_published: int
    engagement_rate: float | None
    has_real_data: bool
    has_demo_data: bool
    source_breakdown: dict[str, Any]


class AnalyticsSyncOut(BaseModel):
    """Result of normalising one payload, including its provenance."""

    metrics: list[AnalyticsMetricOut]
    source: MetricSource
    demo: bool
