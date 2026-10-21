"""Content items, version history, and content scores (Modules 10-12)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
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
from app.models.enums import ContentSource, ContentStatus, ContentType

if TYPE_CHECKING:
    from app.models.business import Business
    from app.models.publishing import Post
    from app.models.recommendation import ContentOpportunity


class Content(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A piece of marketing content at its current revision.

    The live text lives here; every prior revision is a ``ContentVersion`` row.
    That split means the common read path (open the editor) is a single-row fetch,
    while history stays available without bloating it.

    ``source`` is recorded rather than inferred, and upgrades from
    ``ai_generated`` to ``ai_assisted`` the first time a human edits the body.
    Provenance is a spec requirement (section 46) - the UI states plainly whether
    a draft was written by the model, by the user, or both.
    """

    __tablename__ = "content"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    content_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ContentStatus.DRAFT.value, index=True
    )
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ContentSource.AI_GENERATED.value
    )

    title: Mapped[str] = mapped_column(String(500), nullable=False)
    # Markdown for long-form, plain text for social. Rendered to HTML at
    # publish time by the connector, and sanitised before storage.
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    excerpt: Mapped[str | None] = mapped_column(Text)

    # --- SEO fields, for content destined to become a page -----------------
    meta_title: Mapped[str | None] = mapped_column(String(200))
    meta_description: Mapped[str | None] = mapped_column(Text)
    slug: Mapped[str | None] = mapped_column(String(300))
    target_keywords: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    # --- generation provenance ---------------------------------------------
    # The full request that produced this draft: topic, tone, audience,
    # keywords, and which recommendations it was meant to address. Kept so
    # "regenerate" reproduces the same intent, and so the academic write-up can
    # show exactly what the model was asked.
    generation_params: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    ai_model: Mapped[str | None] = mapped_column(String(60))
    generation_tokens: Mapped[int | None] = mapped_column(Integer)
    generated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # True once a human has changed the body.
    edited_by_user: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Latest optimisation score, denormalised so content lists can sort by it.
    latest_score: Mapped[float | None] = mapped_column(Float)

    business: Mapped[Business] = relationship(back_populates="content_items")
    versions: Mapped[list[ContentVersion]] = relationship(
        back_populates="content",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="desc(ContentVersion.version_number)",
    )
    scores: Mapped[list[ContentScore]] = relationship(
        back_populates="content",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="desc(ContentScore.created_at)",
    )
    posts: Mapped[list[Post]] = relationship(
        back_populates="content",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    opportunity: Mapped[ContentOpportunity | None] = relationship(
        back_populates="content", uselist=False
    )

    __table_args__ = (
        Index("ix_content_business_status", "business_id", "status"),
        Index("ix_content_business_type", "business_id", "content_type"),
        Index("ix_content_business_updated", "business_id", "updated_at"),
        CheckConstraint(
            f"content_type IN {ContentType.sql_in_list()}", name="valid_content_type"
        ),
        CheckConstraint(
            f"status IN {ContentStatus.sql_in_list()}", name="valid_content_status"
        ),
        CheckConstraint(
            f"source IN {ContentSource.sql_in_list()}", name="valid_content_source"
        ),
        CheckConstraint("word_count >= 0", name="non_negative_content_words"),
    )


class ContentVersion(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """An immutable snapshot of a content item (Module 12 history).

    A new version is written before every destructive change - user edit, AI
    regeneration, or optimisation rewrite - so nothing the user wrote can be lost
    to a regeneration, and any revision can be restored.
    """

    __tablename__ = "content_versions"

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    meta_title: Mapped[str | None] = mapped_column(String(200))
    meta_description: Mapped[str | None] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # What produced this revision: "generated", "user_edit", "optimized",
    # "regenerated", "restored".
    change_reason: Mapped[str] = mapped_column(String(40), nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    score_at_version: Mapped[float | None] = mapped_column(Float)

    content: Mapped[Content] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint(
            "content_id", "version_number", name="unique_version_per_content"
        ),
        Index("ix_content_versions_content_number", "content_id", "version_number"),
        CheckConstraint("version_number >= 1", name="positive_version_number"),
    )


class ContentScore(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Optimisation analysis of a content item (Module 11).

    Computed by the same kind of deterministic rule logic as the site SEO score,
    applied to a single document: title quality, heading structure, readability,
    keyword usage, FAQ and internal-link opportunities, CTA presence, metadata
    completeness. Scores accumulate over versions so the editor can show that
    optimising actually moved the number.
    """

    __tablename__ = "content_scores"

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    grade: Mapped[str] = mapped_column(String(16), nullable=False)

    # Per-category points, mirroring the SEO engine's shape:
    # {"title": {"earned": 8, "available": 10, "checks": {...}}, ...}
    category_scores: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Actionable findings for this document, each with a rule id and severity.
    issues: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    suggestions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    readability_score: Mapped[float | None] = mapped_column(Float)
    reading_grade_level: Mapped[float | None] = mapped_column(Float)
    keyword_density: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    engine_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default="1.0"
    )

    content: Mapped[Content] = relationship(back_populates="scores")

    __table_args__ = (
        Index("ix_content_scores_content_created", "content_id", "created_at"),
        CheckConstraint(
            "overall_score >= 0 AND overall_score <= 100", name="valid_content_score"
        ),
    )
