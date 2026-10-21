"""User account, preferences, and password-reset tokens."""

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
from app.models.enums import ThemePreference

if TYPE_CHECKING:
    from app.models.assistant import AiConversation
    from app.models.business import Business
    from app.models.system import ActivityLog, Notification


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An AURA account.

    The row exists regardless of which auth provider is active, because every
    other table needs a stable local ``user_id`` foreign key.

    * ``AUTH_PROVIDER=local``    - ``password_hash`` is set, ``supabase_user_id`` is null.
    * ``AUTH_PROVIDER=supabase`` - the reverse. Supabase owns the credential and
      this row is a local mirror created on first authenticated request.

    The CHECK constraint enforces that exactly one of the two is always present,
    so an account can never end up with no way to authenticate.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(320), nullable=False, unique=True, index=True
    )
    full_name: Mapped[str | None] = mapped_column(String(200))

    # Local provider only. Null under Supabase auth.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Supabase provider only. Null under local auth.
    supabase_user_id: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- preferences (Module 24) --------------------------------------------
    theme: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ThemePreference.SYSTEM.value
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # Per-NotificationType opt-out map, e.g. {"publish_failed": false}.
    # Absent keys default to enabled.
    notification_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Defaults applied to AI generation: creativity, length, reading level.
    ai_preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    # --- relationships ------------------------------------------------------
    businesses: Mapped[list[Business]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    notifications: Mapped[list[Notification]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    activity_logs: Mapped[list[ActivityLog]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    conversations: Mapped[list[AiConversation]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reset_tokens: Mapped[list[PasswordResetToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(
            "password_hash IS NOT NULL OR supabase_user_id IS NOT NULL",
            name="has_auth_credential",
        ),
        CheckConstraint(
            "theme IN ('light', 'dark', 'system')",
            name="valid_theme",
        ),
        Index("ix_users_active_email", "is_active", "email"),
    )

    def notification_enabled(self, notification_type: str) -> bool:
        """Opt-out semantics: enabled unless explicitly disabled."""
        return bool(self.notification_preferences.get(notification_type, True))


class UserSession(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One refresh-token grant, so that signing out actually revokes access.

    Local provider only -- under Supabase auth, session lifecycle belongs to
    Supabase and this table stays empty.

    Access tokens are short-lived JWTs and carry this row's id as ``sid``. Every
    authenticated request re-checks that the session is still live, which is what
    makes "sign out" and "sign out everywhere" real server-side revocation rather
    than the client merely forgetting a token.

    Refresh tokens are **rotated**: presenting one revokes it and issues a
    replacement in the same ``family_id`` chain. Because a rotated token is kept
    (marked revoked) rather than deleted, presenting it a second time is
    detectable -- that only happens if a token was captured, so the entire family
    is revoked and the user must sign in again. Only the SHA-256 hash of the
    refresh token is stored; a database leak hands over no usable token.
    """

    __tablename__ = "user_sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # Shared by every rotation of one original login, so reuse of a stolen token
    # can revoke the whole chain rather than just the single presented token.
    family_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )

    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # rotated | logout | logout_all | reuse_detected | password_change
    revoked_reason: Mapped[str | None] = mapped_column(String(32))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Shown in the "active sessions" list so a user can recognise a device.
    # Never used for authorisation -- both are client-controlled.
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        CheckConstraint(
            "revoked_at IS NULL OR revoked_reason IS NOT NULL",
            name="revoked_session_has_reason",
        ),
        Index("ix_user_sessions_user_revoked", "user_id", "revoked_at"),
    )

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_active(self, *, now: datetime) -> bool:
        return self.revoked_at is None and self.expires_at > now

    def revoke(self, reason: str, *, now: datetime) -> None:
        """Idempotent: an already-revoked session keeps its original reason.

        This matters for reuse detection -- overwriting ``rotated`` would erase
        the very marker that identifies a replayed token.
        """
        if self.revoked_at is None:
            self.revoked_at = now
            self.revoked_reason = reason


class PasswordResetToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single-use, time-limited password reset grant (local provider only).

    Only the SHA-256 hash of the token is stored, so a database leak does not
    hand out working reset links. ``used_at`` makes reuse detectable rather than
    just deleting the row, which is useful for the activity log.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    requested_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship(back_populates="reset_tokens")

    def is_usable(self, *, now: datetime) -> bool:
        return self.used_at is None and self.expires_at > now
