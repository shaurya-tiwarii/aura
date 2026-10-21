"""Request and response schemas for business-owned content drafts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import ContentStatus, ContentType


class ContentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ContentType
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=100000)
    excerpt: str | None = Field(default=None, max_length=5000)
    meta_title: str | None = Field(default=None, max_length=200)
    meta_description: str | None = Field(default=None, max_length=5000)
    slug: str | None = Field(default=None, max_length=300)
    target_keywords: list[str] = Field(default_factory=list, max_length=30)


class ContentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ContentType | None = None
    status: ContentStatus | None = None
    title: str | None = Field(default=None, min_length=1, max_length=500)
    body: str | None = Field(default=None, max_length=100000)
    excerpt: str | None = Field(default=None, max_length=5000)
    meta_title: str | None = Field(default=None, max_length=200)
    meta_description: str | None = Field(default=None, max_length=5000)
    slug: str | None = Field(default=None, max_length=300)
    target_keywords: list[str] | None = Field(default=None, max_length=30)


class ContentGenerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: ContentType
    title: str | None = Field(default=None, max_length=500)
    topic: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=12000)
    tone: str = Field(default="clear", min_length=1, max_length=50)
    target_keywords: list[str] = Field(default_factory=list, max_length=30)
    audience: str | None = Field(default=None, max_length=500)
    call_to_action: str | None = Field(default=None, max_length=500)
    opportunity_id: uuid.UUID | None = None


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    content_type: str
    status: str
    source: str
    title: str
    body: str
    excerpt: str | None
    meta_title: str | None
    meta_description: str | None
    slug: str | None
    target_keywords: list[str]
    generation_params: dict[str, object]
    ai_model: str | None
    generation_tokens: int | None
    generated_at: datetime | None
    edited_by_user: bool
    word_count: int
    current_version: int
    latest_score: float | None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def generation_mode(self) -> str:
        """Explicitly distinguish a local demo payload from a live model call."""
        return (
            "demo" if self.ai_model == "demo" else "live" if self.ai_model else "none"
        )


class ContentListOut(BaseModel):
    items: list[ContentOut]
    total: int


class ContentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    version_number: int
    title: str
    body: str
    meta_title: str | None
    meta_description: str | None
    word_count: int
    change_reason: str
    change_summary: str | None
    score_at_version: float | None
    created_at: datetime


class ContentScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    version_number: int
    overall_score: float
    grade: str
    category_scores: dict[str, object]
    issues: list[dict[str, object]]
    suggestions: list[dict[str, object]]
    readability_score: float | None
    reading_grade_level: float | None
    keyword_density: dict[str, object]
    engine_version: str
    created_at: datetime


__all__ = [
    "ContentCreate",
    "ContentGenerate",
    "ContentListOut",
    "ContentOut",
    "ContentUpdate",
    "ContentVersionOut",
    "ContentScoreOut",
]
