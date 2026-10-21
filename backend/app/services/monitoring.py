"""Website monitoring schedule lifecycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.core.errors import ValidationFailedError
from app.models.enums import MonitorFrequency
from app.models.website import Website
from app.schemas.monitoring import MonitoringUpdate


def get_monitoring(website: Website) -> Website:
    return website


def validate_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationFailedError(
            "That monitoring timezone is not available on this system.",
            code="invalid_timezone",
            hint="Use an IANA timezone such as UTC or Asia/Kolkata.",
        ) from exc


def update_monitoring(session: Session, website: Website, payload: MonitoringUpdate) -> Website:
    validate_timezone(payload.timezone)
    website.monitoring_enabled = payload.enabled
    website.monitor_frequency = payload.frequency.value
    website.monitor_hour = payload.hour
    website.monitor_minute = payload.minute
    website.monitor_day_of_week = payload.day_of_week
    website.monitor_timezone = payload.timezone
    if not payload.enabled:
        website.monitor_job_id = None
    session.flush()
    return website


__all__ = ["get_monitoring", "update_monitoring", "validate_timezone"]
