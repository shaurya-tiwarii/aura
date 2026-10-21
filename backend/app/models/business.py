"""Business workspace and its marketing profile (Modules 2 and 3)."""

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
from app.models.enums import OnboardingStep

if TYPE_CHECKING:
    from app.models.assistant import AiConversation
    from app.models.content import Content
    from app.models.publishing import PlatformConnection
    from app.models.recommendation import ContentOpportunity, Recommendation
    from app.models.user import User
    from app.models.website import Website


class Business(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The tenant boundary for all analysis, content, and publishing.

    Every downstream record hangs off a business, and every query is filtered by
    ``business.owner_id`` against the authenticated user. This single hop is what
    enforces user isolation (spec section 20) - there is no path to another
    tenant's data that does not go through here.

    A user may own several businesses (the spec's agency/consultant secondary
    audience), so ``is_default`` marks which one the dashboard opens to.
    """

    __tablename__ = "businesses"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    industry: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(200))
    primary_website_url: Mapped[str | None] = mapped_column(String(2048))

    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Onboarding is resumable, so the furthest-reached step is persisted rather
    # than held in frontend state.
    onboarding_step: Mapped[str | None] = mapped_column(String(40))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- relationships ------------------------------------------------------
    owner: Mapped[User] = relationship(back_populates="businesses")
    profile: Mapped[BusinessProfile | None] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    websites: Mapped[list[Website]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    content_items: Mapped[list[Content]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    recommendations: Mapped[list[Recommendation]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    opportunities: Mapped[list[ContentOpportunity]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    connections: Mapped[list[PlatformConnection]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    conversations: Mapped[list[AiConversation]] = relationship(
        back_populates="business",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_businesses_owner_default", "owner_id", "is_default"),
        UniqueConstraint("owner_id", "name", name="unique_business_name_per_owner"),
        # NULL passes a CHECK, which is what we want: a business that has not
        # started onboarding has no step, and only a *wrong* step is rejected.
        CheckConstraint(
            f"onboarding_step IS NULL OR onboarding_step IN "
            f"{OnboardingStep.sql_in_list()}",
            name="valid_onboarding_step",
        ),
    )

    @property
    def is_onboarded(self) -> bool:
        return self.onboarding_completed_at is not None


class BusinessProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Marketing context used to ground every AI prompt (spec section 23).

    Kept in its own table rather than widening ``businesses`` because these
    fields are large, optional, edited as a unit during onboarding, and read
    together whenever a prompt is assembled.

    The list/dict columns are JSON because they are always consumed whole - the
    prompt builder needs the full set of services or keywords, never a filtered
    subset - so normalising them into child tables would add joins for no query
    benefit. ``keywords`` is the one exception worth noting: keyword *tracking*
    over time is future scope (spec section 44), and would justify promoting it
    to its own table then.
    """

    __tablename__ = "business_profiles"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # --- what the business sells -------------------------------------------
    products: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    services: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    unique_selling_points: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    # --- who it sells to ----------------------------------------------------
    target_audience: Mapped[str | None] = mapped_column(Text)
    audience_segments: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    service_areas: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    # --- how it speaks ------------------------------------------------------
    brand_voice: Mapped[str | None] = mapped_column(String(120))
    brand_tone: Mapped[str | None] = mapped_column(String(120))
    # Explicit "never say this" list, injected as a negative constraint into
    # generation prompts.
    content_restrictions: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    preferred_reading_level: Mapped[str | None] = mapped_column(String(40))

    # --- SEO targets --------------------------------------------------------
    keywords: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    # Social profile URLs, e.g. {"linkedin": "https://...", "instagram": "..."}.
    social_links: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    # --- contact / entity signals ------------------------------------------
    # These feed the AI-visibility "trust" and "entity clarity" categories, where
    # a verifiable real-world identity is the signal being measured.
    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(60))
    founded_year: Mapped[int | None] = mapped_column(Integer)

    business: Mapped[Business] = relationship(back_populates="profile")

    def prompt_context(self) -> dict[str, Any]:
        """Compact, non-empty view of the profile for AI prompt assembly.

        Empty fields are dropped rather than sent as blanks: an empty value in a
        prompt invites the model to invent one, which spec section 23 forbids.
        """
        raw: dict[str, Any] = {
            "products": self.products,
            "services": self.services,
            "unique_selling_points": self.unique_selling_points,
            "target_audience": self.target_audience,
            "audience_segments": self.audience_segments,
            "service_areas": self.service_areas,
            "brand_voice": self.brand_voice,
            "brand_tone": self.brand_tone,
            "content_restrictions": self.content_restrictions,
            "preferred_reading_level": self.preferred_reading_level,
            "keywords": self.keywords,
        }
        return {key: value for key, value in raw.items() if value}
