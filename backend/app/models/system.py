"""Notifications and the activity log (Modules 22-23)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import (
    Base,
    JSONColumn,
    TimestampMixin,
    UTCDateTime,
    UUIDPrimaryKeyMixin,
)
from app.models.enums import ActivityAction, NotificationLevel, NotificationType

if TYPE_CHECKING:
    from app.models.user import User


class Notification(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An in-app notification.

    ``link_path`` is a frontend route rather than a full URL, so a notification
    remains valid across environments and deployments.
    """

    __tablename__ = "notifications"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        index=True,
    )

    notification_type: Mapped[str] = mapped_column(
        String(40), nullable=False, index=True
    )
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=NotificationLevel.INFO.value
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # In-app route to the relevant record, e.g. "/content/<uuid>".
    link_path: Mapped[str | None] = mapped_column(String(500))
    # Ids of the records this refers to, for deduplication and deep linking.
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="notifications")

    __table_args__ = (
        Index("ix_notifications_user_unread", "user_id", "is_read", "created_at"),
        CheckConstraint(
            f"notification_type IN {NotificationType.sql_in_list()}",
            name="valid_notification_type",
        ),
        CheckConstraint(
            f"level IN {NotificationLevel.sql_in_list()}",
            name="valid_notification_level",
        ),
    )


class ActivityLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An append-only audit trail of user and system actions.

    ``metadata_payload`` holds only non-sensitive descriptive data - record ids,
    score values, platform names. Passwords, tokens, and API keys are never
    written here (spec sections 23 and 30); the redaction in
    ``app.core.logging`` covers the log stream, and this column is populated
    explicitly by callers rather than by dumping request bodies.

    Named ``metadata_payload`` because ``metadata`` is reserved on the SQLAlchemy
    declarative base.
    """

    __tablename__ = "activity_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        index=True,
    )

    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # Human-readable one-liner for the activity feed.
    description: Mapped[str] = mapped_column(String(500), nullable=False)

    # What was acted upon: "content", "website", "recommendation", "post".
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))

    metadata_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    # Coarse request provenance for the security view. No full user-agent
    # fingerprinting, and no request bodies.
    ip_address: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="activity_logs")

    __table_args__ = (
        Index("ix_activity_user_created", "user_id", "created_at"),
        Index("ix_activity_business_created", "business_id", "created_at"),
        Index("ix_activity_business_action", "business_id", "action"),
        CheckConstraint(
            f"action IN {ActivityAction.sql_in_list()}", name="valid_activity_action"
        ),
    )
