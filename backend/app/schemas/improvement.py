"""Schemas for deterministic scan-to-scan improvement comparisons."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ImprovementCompareIn(BaseModel):
    """The two immutable scans to compare."""

    model_config = ConfigDict(extra="forbid")

    previous_scan_id: uuid.UUID
    current_scan_id: uuid.UUID


class ImprovementRecordOut(BaseModel):
    """A persisted, evidence-backed comparison of two scans."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    website_id: uuid.UUID
    previous_scan_id: uuid.UUID
    current_scan_id: uuid.UUID
    previous_seo_score: float
    current_seo_score: float
    seo_delta: float
    previous_ai_visibility_score: float
    current_ai_visibility_score: float
    ai_visibility_delta: float
    recommendations_applied: int
    applied_details: list[dict[str, Any]] = Field(default_factory=list)
    new_issues: list[dict[str, Any]] = Field(default_factory=list)
    issues_resolved: int
    issues_introduced: int
    category_deltas: dict[str, Any] = Field(default_factory=dict)
    is_comparable: bool
    incomparable_reason: str | None
    summary: str | None
    created_at: datetime
    updated_at: datetime


__all__ = ["ImprovementCompareIn", "ImprovementRecordOut"]
