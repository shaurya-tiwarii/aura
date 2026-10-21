"""SQLAlchemy models for AURA.

Every model is imported here for two reasons that both matter:

1. **Mapper resolution.** Relationships are declared with string targets
   (``"Business"``, ``"WebsiteScan"``). SQLAlchemy resolves those names only
   against classes it has already seen, so a model that is never imported causes
   an ``InvalidRequestError`` the first time an unrelated query runs.
2. **Alembic autogenerate.** ``alembic/env.py`` imports this module to populate
   ``Base.metadata``. A table missing from here would be silently dropped from
   generated migrations.

Adding a model means adding it below.
"""

from app.models.ai_visibility import (
    HEURISTIC_DISCLAIMER,
    AiVisibilityCategoryScore,
    AiVisibilityReport,
)
from app.models.analytics import AnalyticsMetric, AnalyticsSnapshot
from app.models.assistant import AiConversation, AiMessage
from app.models.base import (
    Base,
    JSONColumn,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    utc_now,
)
from app.models.business import Business, BusinessProfile
from app.models.content import Content, ContentScore, ContentVersion
from app.models.improvement import ImprovementRecord
from app.models.publishing import PlatformConnection, Post, ScheduledPost
from app.models.recommendation import ContentOpportunity, Recommendation
from app.models.seo import SeoCategoryScore, SeoIssue, SeoReport
from app.models.system import ActivityLog, Notification
from app.models.user import PasswordResetToken, User, UserSession
from app.models.website import Website, WebsitePage, WebsiteScan

__all__ = [  # noqa: RUF022 - grouped by domain, not alphabetical; see comments
    # infrastructure
    "Base",
    "JSONColumn",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "utc_now",
    "HEURISTIC_DISCLAIMER",
    # identity
    "User",
    "UserSession",
    "PasswordResetToken",
    # business
    "Business",
    "BusinessProfile",
    # website analysis
    "Website",
    "WebsiteScan",
    "WebsitePage",
    # scoring
    "SeoReport",
    "SeoCategoryScore",
    "SeoIssue",
    "AiVisibilityReport",
    "AiVisibilityCategoryScore",
    # recommendations
    "Recommendation",
    "ContentOpportunity",
    # content
    "Content",
    "ContentVersion",
    "ContentScore",
    # publishing
    "PlatformConnection",
    "Post",
    "ScheduledPost",
    # analytics
    "AnalyticsMetric",
    "AnalyticsSnapshot",
    # assistant
    "AiConversation",
    "AiMessage",
    # system
    "Notification",
    "ActivityLog",
    # improvement loop
    "ImprovementRecord",
]
