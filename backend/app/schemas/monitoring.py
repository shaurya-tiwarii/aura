from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.models.enums import MonitorFrequency


class MonitoringOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    website_id: uuid.UUID
    enabled: bool = Field(validation_alias="monitoring_enabled")
    frequency: str = Field(validation_alias="monitor_frequency")
    hour: int = Field(validation_alias="monitor_hour", ge=0, le=23)
    minute: int = Field(validation_alias="monitor_minute", ge=0, le=59)
    day_of_week: int = Field(validation_alias="monitor_day_of_week", ge=0, le=6)
    timezone: str = Field(validation_alias="monitor_timezone")
    job_id: str | None = Field(validation_alias="monitor_job_id")
    last_run_at: datetime | None = Field(validation_alias="last_monitor_run_at")


class MonitoringUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    frequency: MonitorFrequency = MonitorFrequency.WEEKLY
    hour: int = Field(default=9, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    day_of_week: int = Field(default=0, ge=0, le=6)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


__all__ = ["MonitoringOut", "MonitoringUpdate"]
