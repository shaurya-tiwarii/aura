"""Platform connections, publish attempts, and scheduled jobs (Modules 13-15).

The real/demo distinction is structural here, not cosmetic. Every
``PlatformConnection``, ``Post``, and ``ScheduledPost`` carries a ``mode`` column
(``live`` or ``demo``) that is set at creation from whether real API credentials
exist, and is never mutated afterwards. A demo post therefore cannot be rendered
as a real one anywhere in the system, because the flag travels with the row into
every API response (spec sections 14, 25, 46).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
from app.models.enums import (
    ConnectionMode,
    ConnectionStatus,
    Platform,
    PostStatus,
)

if TYPE_CHECKING:
    from app.models.analytics import AnalyticsMetric
    from app.models.business import Business
    from app.models.content import Content


class PlatformConnection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A business's link to one publishing platform.

    Tokens are stored Fernet-encrypted (see ``app.core.security.TokenVault``) and
    are never returned by the API - the settings UI receives only
    ``account_name`` and a masked hint. In demo mode the token columns stay null,
    which makes "demo connection" unforgeable rather than a matter of trusting a
    flag alone.
    """

    __tablename__ = "platform_connections"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    platform: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ConnectionMode.DEMO.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ConnectionStatus.DISCONNECTED.value
    )

    # --- identity on the remote platform (safe to display) -----------------
    account_name: Mapped[str | None] = mapped_column(String(200))
    account_id: Mapped[str | None] = mapped_column(String(200))
    account_url: Mapped[str | None] = mapped_column(String(2048))
    # WordPress site id, Reddit subreddit, LinkedIn organisation URN.
    target_identifier: Mapped[str | None] = mapped_column(String(300))

    # --- credentials (encrypted at rest, never serialised) -----------------
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    granted_scopes: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    connected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Non-secret platform metadata: available subreddits, site capabilities.
    platform_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    business: Mapped[Business] = relationship(back_populates="connections")
    posts: Mapped[list[Post]] = relationship(
        back_populates="connection", passive_deletes=True
    )

    __table_args__ = (
        UniqueConstraint(
            "business_id", "platform", name="unique_connection_per_platform"
        ),
        Index("ix_connections_business_status", "business_id", "status"),
        CheckConstraint(f"platform IN {Platform.sql_in_list()}", name="valid_platform"),
        CheckConstraint(
            f"mode IN {ConnectionMode.sql_in_list()}", name="valid_connection_mode"
        ),
        CheckConstraint(
            f"status IN {ConnectionStatus.sql_in_list()}",
            name="valid_connection_status",
        ),
        # A live connection must actually hold a credential. This is the
        # constraint that makes it impossible to mark a connection "live" while
        # it is in fact simulating.
        CheckConstraint(
            "mode = 'demo' OR access_token_encrypted IS NOT NULL",
            name="live_connection_requires_token",
        ),
    )

    @property
    def is_usable(self) -> bool:
        return self.status == ConnectionStatus.CONNECTED.value

    @property
    def is_demo(self) -> bool:
        return self.mode == ConnectionMode.DEMO.value


class Post(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One publish attempt of a content item to one platform.

    A content item published to three platforms produces three posts, each with
    its own status, remote id, and analytics. Failed attempts are kept, not
    deleted, so the retry history is visible and the content is never lost
    (spec section 26).
    """

    __tablename__ = "posts"

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("platform_connections.id", ondelete="SET NULL"),
        index=True,
    )

    platform: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default=ConnectionMode.DEMO.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PostStatus.DRAFT.value, index=True
    )

    # --- remote result ------------------------------------------------------
    # Null in demo mode; a real platform id when live. Their presence or absence
    # is the ground truth for whether a publish really happened.
    remote_post_id: Mapped[str | None] = mapped_column(String(300))
    remote_url: Mapped[str | None] = mapped_column(String(2048))

    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The exact payload sent, minus credentials. Useful for debugging a rejected
    # post and for the viva demonstration.
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    response_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    content: Mapped[Content] = relationship(back_populates="posts")
    connection: Mapped[PlatformConnection | None] = relationship(back_populates="posts")
    scheduled_post: Mapped[ScheduledPost | None] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    metrics: Mapped[list[AnalyticsMetric]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_posts_platform_status", "platform", "status"),
        Index("ix_posts_content_platform", "content_id", "platform"),
        CheckConstraint(
            f"platform IN {Platform.sql_in_list()}", name="valid_post_platform"
        ),
        CheckConstraint(
            f"mode IN {ConnectionMode.sql_in_list()}", name="valid_post_mode"
        ),
        CheckConstraint(
            f"status IN {PostStatus.sql_in_list()}", name="valid_post_status"
        ),
        # A live post that claims to be published must carry a remote id.
        CheckConstraint(
            "status <> 'published' OR mode = 'demo' OR remote_post_id IS NOT NULL",
            name="published_live_post_requires_remote_id",
        ),
    )

    @property
    def is_simulated(self) -> bool:
        return self.mode == ConnectionMode.DEMO.value


class ScheduledPost(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A queued future publish, owned by APScheduler (Module 15).

    The database is the source of truth, not the scheduler's own job store: on
    restart, pending rows are re-registered. Without that, a process restart
    would silently drop every scheduled post - the kind of quiet failure the
    spec's no-fake-functionality rule is aimed at.
    """

    __tablename__ = "scheduled_posts"

    post_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("posts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    scheduled_for: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, index=True
    )
    timezone_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PostStatus.SCHEDULED.value, index=True
    )

    # APScheduler job id, so a cancel can remove the live trigger too.
    job_id: Mapped[str | None] = mapped_column(String(120), unique=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(Text)
    # True once the "post going out soon" notification has been sent, so it is
    # not sent repeatedly by every scheduler tick.
    reminder_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    post: Mapped[Post] = relationship(back_populates="scheduled_post")

    __table_args__ = (
        Index("ix_scheduled_posts_due", "status", "scheduled_for"),
        CheckConstraint(
            f"status IN {PostStatus.sql_in_list()}", name="valid_scheduled_status"
        ),
        CheckConstraint("retry_count >= 0", name="non_negative_retry_count"),
        CheckConstraint("retry_count <= max_retries", name="retry_within_limit"),
    )

    @property
    def can_retry(self) -> bool:
        # The scheduler keeps a retrying row SCHEDULED (with next_retry_at set)
        # until the budget is exhausted; FAILED is retained so a manually
        # retried row still reports correctly.
        return (
            self.status
            in (PostStatus.SCHEDULED.value, PostStatus.FAILED.value)
            and self.retry_count < self.max_retries
        )
