"""AI assistant conversations and messages (Module 19)."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONColumn, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import MessageRole

if TYPE_CHECKING:
    from app.models.business import Business
    from app.models.user import User


class AiConversation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A conversation thread with the marketing assistant.

    Scoped to both ``user_id`` and ``business_id``. The double key is
    intentional: the assistant is grounded in one business's scans, scores, and
    recommendations, and every read is filtered on both columns so context cannot
    cross a tenant boundary (spec sections 19 and 22).
    """

    __tablename__ = "ai_conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Derived from the opening question, so the sidebar is readable.
    title: Mapped[str] = mapped_column(
        String(300), nullable=False, default="New conversation"
    )
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    user: Mapped[User] = relationship(back_populates="conversations")
    business: Mapped[Business] = relationship(back_populates="conversations")
    messages: Mapped[list[AiMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="AiMessage.created_at",
    )

    __table_args__ = (
        Index("ix_conversations_user_business", "user_id", "business_id"),
        Index("ix_conversations_business_updated", "business_id", "updated_at"),
    )


class AiMessage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One turn in a conversation.

    ``grounding`` records which real records were injected into the prompt for
    this turn - scan id, score values, recommendation ids. It is what makes an
    assistant answer auditable after the fact: the claim "your metadata score is
    6/10" can be traced back to the row it came from, rather than being taken on
    trust from the model.
    """

    __tablename__ = "ai_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Which business records were supplied as context for this turn.
    grounding: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Structured actions the assistant proposed (e.g. "generate a FAQ about X"),
    # which the UI renders as buttons. Always confirmed by the user before
    # anything runs - the model never triggers an action directly (section 22).
    suggested_actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    model: Mapped[str | None] = mapped_column(String(60))
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Set when generation failed, so a broken turn is visible in the thread
    # instead of silently missing.
    error_message: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped[AiConversation] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        CheckConstraint(
            f"role IN {MessageRole.sql_in_list()}", name="valid_message_role"
        ),
    )
