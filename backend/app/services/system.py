"""Persistence services for notifications, activity history, and settings."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.enums import ActivityAction, NotificationLevel, NotificationType
from app.models.system import ActivityLog, Notification
from app.models.user import User
from app.schemas.system import SettingsUpdate


def list_notifications(
    session: Session,
    user: User,
    *,
    unread_only: bool = False,
    notification_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int, int]:
    base = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        base = base.where(Notification.is_read.is_(False))
    if notification_type is not None:
        base = base.where(Notification.notification_type == notification_type)
    items = list(
        session.scalars(
            base.order_by(Notification.created_at.desc()).offset(offset).limit(limit)
        )
    )
    total = (
        session.scalar(
            select(func.count(Notification.id)).where(Notification.user_id == user.id)
        )
        or 0
    )
    unread = (
        session.scalar(
            select(func.count(Notification.id)).where(
                Notification.user_id == user.id, Notification.is_read.is_(False)
            )
        )
        or 0
    )
    return items, total, unread


def get_notification(
    session: Session, user: User, notification_id: uuid.UUID
) -> Notification | None:
    return session.scalar(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user.id
        )
    )


def mark_read(notification: Notification) -> None:
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)


def mark_all_read(session: Session, user: User) -> int:
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=datetime.now(UTC))
    )
    return int(result.rowcount)


def list_activity(
    session: Session,
    user: User,
    *,
    business_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ActivityLog], int]:
    conditions = [ActivityLog.user_id == user.id]
    if business_id is not None:
        conditions.append(ActivityLog.business_id == business_id)
    if action is not None:
        conditions.append(ActivityLog.action == action)
    items = list(
        session.scalars(
            select(ActivityLog)
            .where(*conditions)
            .order_by(ActivityLog.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    total = session.scalar(select(func.count(ActivityLog.id)).where(*conditions)) or 0
    return items, total


def update_settings(session: Session, user: User, payload: SettingsUpdate) -> User:
    changes = payload.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if value is not None and hasattr(user, key):
            setattr(user, key, value.value if hasattr(value, "value") else value)
    return user


def create_notification(
    session: Session,
    user: User,
    *,
    notification_type: NotificationType,
    level: NotificationLevel,
    title: str,
    message: str,
    business_id: uuid.UUID | None = None,
    link_path: str | None = None,
    context: dict | None = None,
) -> Notification | None:
    """Create an enabled notification; an explicit ``None`` means opted out."""
    if not user.notification_enabled(notification_type.value):
        return None
    item = Notification(
        user_id=user.id,
        business_id=business_id,
        notification_type=notification_type.value,
        level=level.value,
        title=title,
        message=message,
        link_path=link_path,
        context=context or {},
    )
    session.add(item)
    return item


def record_activity(
    session: Session,
    *,
    user: User | None,
    action: ActivityAction,
    description: str,
    business_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    metadata_payload: dict | None = None,
    ip_address: str | None = None,
    request_id: str | None = None,
) -> ActivityLog:
    item = ActivityLog(
        user_id=user.id if user else None,
        business_id=business_id,
        action=action.value,
        description=description,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata_payload=metadata_payload or {},
        ip_address=ip_address,
        request_id=request_id,
    )
    session.add(item)
    return item
