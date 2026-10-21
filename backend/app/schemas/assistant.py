"""Schemas for the tenant-scoped AI marketing assistant."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AssistantMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=6000)


class AssistantMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    role: str
    content: str
    grounding: dict[str, Any]
    suggested_actions: list[dict[str, Any]]
    model: str | None
    tokens_used: int | None
    latency_ms: int | None
    error_message: str | None
    created_at: datetime


class AssistantConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    user_id: uuid.UUID
    business_id: uuid.UUID
    title: str
    message_count: int
    total_tokens: int
    created_at: datetime
    updated_at: datetime
    messages: list[AssistantMessageOut] = Field(default_factory=list)


class AssistantConversationListOut(BaseModel):
    items: list[AssistantConversationOut] = Field(default_factory=list)
