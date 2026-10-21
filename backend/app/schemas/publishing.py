"""Business-scoped publishing request and response models."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import Platform


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    account_name: str | None = Field(default=None, max_length=200)
    account_id: str | None = Field(default=None, max_length=200)
    account_url: str | None = Field(default=None, max_length=2048)
    target_identifier: str | None = Field(default=None, max_length=300)
    access_token: str | None = Field(default=None, min_length=1, max_length=10000)
    refresh_token: str | None = Field(default=None, min_length=1, max_length=10000)
    token_expires_at: datetime | None = None
    granted_scopes: list[str] = Field(default_factory=list, max_length=50)


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    platform: str
    mode: str
    status: str
    account_name: str | None
    account_id: str | None
    account_url: str | None
    target_identifier: str | None
    granted_scopes: list[str]
    connected_at: datetime | None
    last_used_at: datetime | None
    last_error: str | None
    platform_metadata: dict[str, object]


class ConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str | None = Field(default=None, max_length=200)
    account_id: str | None = Field(default=None, max_length=200)
    account_url: str | None = Field(default=None, max_length=2048)
    target_identifier: str | None = Field(default=None, max_length=300)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: uuid.UUID
    connection_id: uuid.UUID


class ScheduleRequest(PublishRequest):
    scheduled_for: datetime
    timezone_name: str = Field(default="UTC", min_length=1, max_length=64)


class PostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content_id: uuid.UUID
    connection_id: uuid.UUID | None
    platform: str
    mode: str
    status: str
    remote_post_id: str | None
    remote_url: str | None
    published_at: datetime | None
    request_payload: dict[str, object]
    response_payload: dict[str, object]
    error_code: str | None
    error_message: str | None
    attempt_count: int


class ScheduledPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    post_id: uuid.UUID
    scheduled_for: datetime
    timezone_name: str
    status: str
    job_id: str | None
    retry_count: int
    max_retries: int
    error_message: str | None
