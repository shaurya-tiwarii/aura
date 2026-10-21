"""Schemas for notifications, activity history, and account settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import NotificationType, ThemePreference


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID | None
    notification_type: str
    level: str
    title: str
    message: str
    link_path: str | None
    context: dict[str, Any]
    is_read: bool
    read_at: datetime | None
    created_at: datetime
    updated_at: datetime


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int
    unread: int


class NotificationReadAllOut(BaseModel):
    marked_read: int


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID | None
    action: str
    description: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    metadata_payload: dict[str, Any]
    created_at: datetime


class ActivityListOut(BaseModel):
    items: list[ActivityOut]
    total: int


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    theme: ThemePreference
    timezone: str
    notification_preferences: dict[str, bool]
    ai_preferences: dict[str, Any]


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    theme: ThemePreference | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    notification_preferences: dict[str, bool] | None = None
    ai_preferences: dict[str, Any] | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone.") from exc
        return value

    @field_validator("notification_preferences")
    @classmethod
    def validate_notification_preferences(
        cls, value: dict[str, bool] | None
    ) -> dict[str, bool] | None:
        if value is not None:
            unknown = set(value) - set(NotificationType.values())
            if unknown:
                raise ValueError(
                    f"Unknown notification preference(s): {', '.join(sorted(unknown))}."
                )
        return value

    @field_validator("ai_preferences")
    @classmethod
    def validate_ai_preferences(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        allowed = {"creativity", "length", "reading_level"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unknown AI preference(s): {', '.join(sorted(unknown))}.")
        if "creativity" in value and (
            not isinstance(value["creativity"], int | float)
            or isinstance(value["creativity"], bool)
            or not 0 <= value["creativity"] <= 1
        ):
            raise ValueError("creativity must be a number between 0 and 1.")
        if "length" in value and value["length"] not in {"short", "medium", "long"}:
            raise ValueError("length must be one of: short, medium, long.")
        if "reading_level" in value and (
            not isinstance(value["reading_level"], str)
            or not value["reading_level"].strip()
            or len(value["reading_level"]) > 50
        ):
            raise ValueError("reading_level must be a non-empty short label.")
        return value


__all__ = [
    "ActivityListOut",
    "ActivityOut",
    "NotificationListOut",
    "NotificationOut",
    "NotificationReadAllOut",
    "SettingsOut",
    "SettingsUpdate",
]
