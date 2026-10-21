"""Business-scoped platform connections and publishing actions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.deps import DbSession, OwnedBusiness
from app.core.errors import PublishFailedError
from app.core.logging import get_logger
from app.models.publishing import Post
from app.schemas.publishing import (
    ConnectionCreate,
    ConnectionOut,
    ConnectionUpdate,
    PostOut,
    PublishRequest,
    ScheduledPostOut,
    ScheduleRequest,
)
from app.services import publishing

log = get_logger(__name__)

router = APIRouter(prefix="/businesses/{business_id}/publishing", tags=["publishing"])


@router.get("/posts", response_model=list[PostOut])
def list_posts(business: OwnedBusiness, session: DbSession) -> list[PostOut]:
    return [PostOut.model_validate(i) for i in publishing.list_posts(session, business)]


@router.get("/scheduled", response_model=list[ScheduledPostOut])
def list_scheduled(business: OwnedBusiness, session: DbSession) -> list[ScheduledPostOut]:
    return [
        ScheduledPostOut.model_validate(i)
        for i in publishing.list_scheduled(session, business)
    ]


@router.get("/connections", response_model=list[ConnectionOut])
def list_connections(
    business: OwnedBusiness, session: DbSession
) -> list[ConnectionOut]:
    return [
        ConnectionOut.model_validate(i)
        for i in publishing.list_connections(session, business)
    ]


@router.post(
    "/connections", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED
)
def create_connection(
    payload: ConnectionCreate, business: OwnedBusiness, session: DbSession
) -> ConnectionOut:
    item = publishing.create_connection(session, business, payload)
    session.commit()
    session.refresh(item)
    return ConnectionOut.model_validate(item)


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
def update_connection(
    connection_id: uuid.UUID,
    payload: ConnectionUpdate,
    business: OwnedBusiness,
    session: DbSession,
) -> ConnectionOut:
    item = publishing.update_connection(session, business, connection_id, payload)
    session.commit()
    session.refresh(item)
    return ConnectionOut.model_validate(item)


@router.delete("/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> Response:
    publishing.delete_connection(session, business, connection_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/posts/publish", response_model=PostOut, status_code=status.HTTP_201_CREATED
)
def publish_post(
    payload: PublishRequest,
    business: OwnedBusiness,
    session: DbSession,
    response: Response,
) -> PostOut:
    try:
        item = publishing.publish_content(
            session, business, payload.content_id, payload.connection_id
        )
    except PublishFailedError as exc:
        # _publish_post records the failed attempt on the post row before
        # raising. The route owns the transaction: commit that record here so
        # the failure is persisted instead of being rolled back with the
        # exception, then return the failed post (200, not 201) so the client
        # can show what happened without losing the attempt history.
        session.commit()
        item = session.get(Post, exc.post_id) if exc.post_id is not None else None
        if item is None:  # pragma: no cover - the row was just written
            raise
        session.refresh(item)
        response.status_code = status.HTTP_200_OK
        log.warning(
            "publish_post_failed", post_id=str(item.id), error=item.error_message
        )
        return PostOut.model_validate(item)
    session.commit()
    session.refresh(item)
    return PostOut.model_validate(item)


@router.post(
    "/posts/schedule",
    response_model=ScheduledPostOut,
    status_code=status.HTTP_201_CREATED,
)
def schedule_post(
    payload: ScheduleRequest, business: OwnedBusiness, session: DbSession
) -> ScheduledPostOut:
    item = publishing.schedule_content(
        session,
        business,
        payload.content_id,
        payload.connection_id,
        payload.scheduled_for,
        payload.timezone_name,
    )
    session.commit()
    session.refresh(item)
    return ScheduledPostOut.model_validate(item)
