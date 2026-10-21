"""Authenticated notifications, activity history, and account settings."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession
from app.core.errors import NotFoundError
from app.models.enums import ActivityAction, NotificationType
from app.schemas.system import (
    ActivityListOut,
    ActivityOut,
    NotificationListOut,
    NotificationOut,
    NotificationReadAllOut,
    SettingsOut,
    SettingsUpdate,
)
from app.services import system as system_service

router = APIRouter(tags=["system"])


@router.get("/notifications", response_model=NotificationListOut)
def list_notifications(
    user: CurrentUser,
    session: DbSession,
    unread_only: bool = Query(False),
    notification_type: NotificationType | None = Query(None),  # noqa: B008
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> NotificationListOut:
    items, total, unread = system_service.list_notifications(
        session,
        user,
        unread_only=unread_only,
        notification_type=notification_type.value if notification_type else None,
        limit=limit,
        offset=offset,
    )
    return NotificationListOut(
        items=[NotificationOut.model_validate(item) for item in items],
        total=total,
        unread=unread,
    )


@router.post(
    "/notifications/read-all",
    response_model=NotificationReadAllOut,
    status_code=status.HTTP_200_OK,
)
def read_all_notifications(
    user: CurrentUser, session: DbSession
) -> NotificationReadAllOut:
    count = system_service.mark_all_read(session, user)
    session.commit()
    return NotificationReadAllOut(marked_read=count)


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationOut,
)
def read_notification(
    notification_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> NotificationOut:
    item = system_service.get_notification(session, user, notification_id)
    if item is None:
        raise NotFoundError("We could not find that notification.")
    system_service.mark_read(item)
    session.commit()
    session.refresh(item)
    return NotificationOut.model_validate(item)


@router.get("/activity", response_model=ActivityListOut)
def list_activity(
    user: CurrentUser,
    session: DbSession,
    business_id: uuid.UUID | None = Query(None),  # noqa: B008
    action: ActivityAction | None = Query(None),  # noqa: B008
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ActivityListOut:
    items, total = system_service.list_activity(
        session,
        user,
        business_id=business_id,
        action=action.value if action else None,
        limit=limit,
        offset=offset,
    )
    return ActivityListOut(
        items=[ActivityOut.model_validate(item) for item in items],
        total=total,
    )


@router.get("/settings", response_model=SettingsOut)
def get_settings(user: CurrentUser) -> SettingsOut:
    return SettingsOut.model_validate(user)


@router.patch("/settings", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate, user: CurrentUser, session: DbSession
) -> SettingsOut:
    system_service.update_settings(session, user, payload)
    session.commit()
    session.refresh(user)
    return SettingsOut.model_validate(user)
