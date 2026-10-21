from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OpportunityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    title: str
    suggested_content_type: str
    description: str | None
    rationale: str
    source: str
    target_keywords: list[str]
    suggested_outline: list[str]
    impact: str
    effort: str
    priority_score: float
    status: str
    content_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class OpportunityStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern="^(suggested|accepted|dismissed)$")
