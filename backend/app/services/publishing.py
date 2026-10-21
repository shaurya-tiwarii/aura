"""Tenant-scoped publishing and scheduling orchestration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.base import ConnectorError
from app.connectors.demo import DemoConnector
from app.connectors.linkedin import LinkedInConnector
from app.connectors.reddit import RedditConnector
from app.connectors.wordpress import WordPressConnector
from app.core.config import settings
from app.core.errors import (
    ConnectorAuthError,
    ConnectorNotConfiguredError,
    NotFoundError,
    PublishFailedError,
    ValidationFailedError,
)
from app.core.security import get_token_vault
from app.models.business import Business
from app.models.content import Content
from app.models.enums import (
    ActivityAction,
    ConnectionMode,
    ConnectionStatus,
    NotificationLevel,
    NotificationType,
    Platform,
    PostStatus,
)
from app.models.publishing import PlatformConnection, Post, ScheduledPost
from app.schemas.publishing import ConnectionCreate, ConnectionUpdate


def _notify_publish_outcome(
    session: Session,
    business: Business,
    post: Post,
    *,
    succeeded: bool,
    error: str | None = None,
) -> None:
    """Fire publish success/failure events. Additive only: a notification bug
    must never roll back a publish that actually happened."""
    try:
        from app.services import system as system_service

        user = business.owner
        if succeeded:
            system_service.create_notification(
                session,
                user,
                notification_type=NotificationType.PUBLISH_SUCCEEDED,
                level=NotificationLevel.SUCCESS,
                title="Post published",
                message=f"Your post went live on {post.platform}.",
                business_id=business.id,
                link_path="/dashboard?section=Publishing",
                context={"post_id": str(post.id), "platform": post.platform},
            )
            system_service.record_activity(
                session,
                user=user,
                action=ActivityAction.CONTENT_PUBLISHED,
                description=f"Post published to {post.platform}.",
                business_id=business.id,
                entity_type="post",
                entity_id=post.id,
            )
        else:
            system_service.create_notification(
                session,
                user,
                notification_type=NotificationType.PUBLISH_FAILED,
                level=NotificationLevel.ERROR,
                title="Publishing failed",
                message=(
                    "The post could not be published"
                    + (f": {error}" if error else ".")
                    + " It has been kept so nothing is lost."
                ),
                business_id=business.id,
                link_path="/dashboard?section=Publishing",
                context={"post_id": str(post.id), "platform": post.platform},
            )
    except Exception:  # noqa: BLE001 - events must never break publishing
        from app.core.logging import get_logger

        get_logger(__name__).error(
            "publish_notification_failed", post_id=str(post.id), exc_info=True
        )


def list_connections(session: Session, business: Business) -> list[PlatformConnection]:
    return list(
        session.scalars(
            select(PlatformConnection)
            .where(PlatformConnection.business_id == business.id)
            .order_by(PlatformConnection.platform)
        )
    )


def get_connection(
    session: Session, business: Business, connection_id: uuid.UUID
) -> PlatformConnection:
    item = session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.id == connection_id,
            PlatformConnection.business_id == business.id,
        )
    )
    if item is None:
        raise NotFoundError("That platform connection could not be found.")
    return item


def create_connection(
    session: Session, business: Business, payload: ConnectionCreate
) -> PlatformConnection:
    platform = payload.platform.value
    if platform not in {p.value for p in Platform if p.has_live_connector}:
        raise ValidationFailedError(
            "That platform is not available for publishing yet.",
            hint="Use WordPress, LinkedIn, or Reddit.",
        )
    for candidate in (payload.account_url,):
        if candidate and (urlsplit(candidate).username or urlsplit(candidate).password):
            raise ValidationFailedError(
                "Connection URLs must not contain credentials.",
                hint="Store platform credentials in the connection fields instead.",
            )
    existing = session.scalar(
        select(PlatformConnection).where(
            PlatformConnection.business_id == business.id,
            PlatformConnection.platform == platform,
        )
    )
    if existing is not None:
        raise ValidationFailedError("A connection for that platform already exists.")
    live = bool(payload.access_token)
    vault = get_token_vault()
    if live and not vault.enabled:
        raise ConnectorNotConfiguredError(
            "Token encryption is not configured; the credential was not stored.",
            hint="Set TOKEN_ENCRYPTION_KEY before adding a live connection.",
        )
    if live:
        mode, status = ConnectionMode.LIVE.value, ConnectionStatus.CONNECTED.value
        access = vault.encrypt(payload.access_token or "")
        refresh = vault.encrypt_optional(payload.refresh_token)
        connected_at = datetime.now(UTC)
    else:
        mode, status, access, refresh, connected_at = (
            ConnectionMode.DEMO.value,
            ConnectionStatus.CONNECTED.value,
            None,
            None,
            None,
        )
    item = PlatformConnection(
        business_id=business.id,
        platform=platform,
        mode=mode,
        status=status,
        account_name=payload.account_name or f"Demo {platform.title()} account",
        account_id=payload.account_id,
        account_url=payload.account_url,
        target_identifier=payload.target_identifier,
        access_token_encrypted=access,
        refresh_token_encrypted=refresh,
        token_expires_at=payload.token_expires_at,
        granted_scopes=payload.granted_scopes,
        connected_at=connected_at,
        platform_metadata={"demo": not live, "credential_stored": live},
    )
    session.add(item)
    session.flush()
    return item


def delete_connection(
    session: Session, business: Business, connection_id: uuid.UUID
) -> None:
    item = get_connection(session, business, connection_id)
    session.delete(item)


def update_connection(
    session: Session,
    business: Business,
    connection_id: uuid.UUID,
    payload: ConnectionUpdate,
) -> PlatformConnection:
    item = get_connection(session, business, connection_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    session.flush()
    return item


def _connector(connection: PlatformConnection):
    if connection.is_demo:
        return DemoConnector(connection.platform)
    if not connection.access_token_encrypted:
        raise ConnectorAuthError()
    token = get_token_vault().decrypt(connection.access_token_encrypted)
    if connection.platform == Platform.WORDPRESS.value:
        if not connection.account_url:
            raise ValidationFailedError("WordPress requires the site URL.")
        return WordPressConnector(token, connection.account_url)
    if connection.platform == Platform.LINKEDIN.value:
        return LinkedInConnector(token)
    return RedditConnector(token, settings.reddit_user_agent)


def publish_content(
    session: Session,
    business: Business,
    content_id: uuid.UUID,
    connection_id: uuid.UUID,
) -> Post:
    content = session.scalar(
        select(Content).where(
            Content.id == content_id, Content.business_id == business.id
        )
    )
    if content is None:
        raise NotFoundError("That content draft could not be found.")
    connection = get_connection(session, business, connection_id)
    if not connection.is_usable:
        raise ConnectorNotConfiguredError("That platform connection is not usable.")
    post = Post(
        content_id=content.id,
        connection_id=connection.id,
        platform=connection.platform,
        mode=connection.mode,
        status=PostStatus.PUBLISHING.value,
        request_payload={"title": content.title, "body": content.body},
        attempt_count=1,
    )
    session.add(post)
    _publish_post(session, business, connection, content, post)
    session.flush()
    return post


def _publish_post(
    session: Session,
    business: Business,
    connection: PlatformConnection,
    content: Content,
    post: Post,
) -> None:
    try:
        result = _connector(connection).publish(
            title=content.title,
            body=content.body,
            target_identifier=connection.target_identifier,
        )
        post.status = PostStatus.PUBLISHED.value
        post.remote_post_id, post.remote_url = result.remote_post_id, result.remote_url
        post.response_payload = result.response_payload
        post.published_at = datetime.now(UTC)
        connection.last_used_at = post.published_at
    except (ConnectorError, RuntimeError) as exc:
        post.status, post.error_code = PostStatus.FAILED.value, "publish_failed"
        post.error_message = str(exc)
        _notify_publish_outcome(session, business, post, succeeded=False, error=str(exc))
        # The failed attempt is recorded on the post row above; the exception
        # carries the post id so the route (which owns the transaction) can
        # commit that record instead of letting the raise roll it back.
        raise PublishFailedError(str(exc), post_id=post.id) from exc
    _notify_publish_outcome(session, business, post, succeeded=True)
    session.flush()


def schedule_content(
    session: Session,
    business: Business,
    content_id: uuid.UUID,
    connection_id: uuid.UUID,
    scheduled_for: datetime,
    timezone_name: str,
) -> ScheduledPost:
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=UTC)
    else:
        scheduled_for = scheduled_for.astimezone(UTC)
    if scheduled_for <= datetime.now(UTC):
        raise ValidationFailedError("Scheduled time must be in the future.")
    content = session.scalar(
        select(Content).where(
            Content.id == content_id, Content.business_id == business.id
        )
    )
    if content is None:
        raise NotFoundError("That content draft could not be found.")
    connection = get_connection(session, business, connection_id)
    post = Post(
        content_id=content.id,
        connection_id=connection.id,
        platform=connection.platform,
        mode=connection.mode,
        status=PostStatus.SCHEDULED.value,
        request_payload={"title": content.title, "body": content.body},
    )
    scheduled = ScheduledPost(
        post=post,
        scheduled_for=scheduled_for,
        timezone_name=timezone_name,
        max_retries=settings.scheduler_max_retries,
    )
    session.add(scheduled)
    session.flush()
    # Register immediately for a running process.  The scheduler module is
    # imported lazily to avoid the publishing <-> scheduler worker import cycle;
    # its startup reconciliation still guarantees recovery after a restart.
    from app.scheduler.service import register_scheduled

    register_scheduled(scheduled)
    return scheduled


def publish_existing_post(session: Session, business: Business, post: Post) -> Post:
    """Publish a row created by the scheduler without creating a duplicate."""
    content = session.scalar(
        select(Content).where(
            Content.id == post.content_id, Content.business_id == business.id
        )
    )
    if content is None or post.connection_id is None:
        raise NotFoundError("The scheduled post content or connection was removed.")
    connection = get_connection(session, business, post.connection_id)
    post.status = PostStatus.PUBLISHING.value
    post.attempt_count += 1
    _publish_post(session, business, connection, content, post)
    return post


def list_posts(session: Session, business: Business) -> list[Post]:
    return list(
        session.scalars(
            select(Post)
            .join(Content, Content.id == Post.content_id)
            .where(Content.business_id == business.id)
            .order_by(Post.created_at.desc())
        )
    )


def list_scheduled(session: Session, business: Business) -> list[ScheduledPost]:
    return list(
        session.scalars(
            select(ScheduledPost)
            .join(ScheduledPost.post)
            .join(Post.content)
            .where(Content.business_id == business.id)
            .order_by(ScheduledPost.scheduled_for.asc())
        )
    )
