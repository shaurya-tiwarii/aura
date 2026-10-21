"""Shared enumerations.

These are stored as strings rather than native database enums. Native Postgres
enums require a migration to add a value, which would make adding a content type
or platform a schema change; string columns with a Python-side enum plus a CHECK
constraint give the same validation with far less friction.

Every enum inherits ``str`` so instances serialise directly to JSON and compare
equal to their wire value.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-valued enum with a readable ``str()``."""

    def __str__(self) -> str:
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]

    @classmethod
    def sql_in_list(cls) -> str:
        """Render the members as a SQL ``IN`` list: ``('a', 'b', 'c')``.

        Used to build CHECK constraints from the enum, so the database and the
        Python code can never disagree about the allowed values.

        Why not ``f"IN {tuple(cls.values())}"``: Python renders a single-element
        tuple as ``('a',)``, and the trailing comma is a syntax error in SQL.
        That would only break for an enum with exactly one member - which is
        exactly the kind of latent trap that surfaces months later when someone
        adds one.
        """
        quoted = ", ".join(f"'{value}'" for value in cls.values())
        return f"({quoted})"


# =============================================================================
# Businesses and onboarding
# =============================================================================
class OnboardingStep(StrEnum):
    """Where a business is in the guided setup flow.

    Persisted on the business rather than kept in frontend state so setup is
    resumable across devices and browser sessions. ``COMPLETE`` is the step
    label; the authoritative "is it finished" signal is
    ``Business.onboarding_completed_at`` being set, because a timestamp cannot be
    reached by accident the way a string can.
    """

    BUSINESS_BASICS = "business_basics"
    WEBSITE = "website"
    PROFILE = "profile"
    FIRST_SCAN = "first_scan"
    COMPLETE = "complete"


# =============================================================================
# Scanning and analysis
# =============================================================================
class MonitorFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


class ScanStatus(StrEnum):
    QUEUED = "queued"
    CRAWLING = "crawling"
    ANALYZING = "analyzing"
    SCORING = "scoring"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"  # some pages fetched, some failed


class ScanTrigger(StrEnum):
    MANUAL = "manual"
    ONBOARDING = "onboarding"
    SCHEDULED = "scheduled"
    REANALYSIS = "reanalysis"  # improvement-loop re-scan


class PageFetchStatus(StrEnum):
    OK = "ok"
    REDIRECTED = "redirected"
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"  # robots.txt or 403
    SKIPPED = "skipped"  # non-HTML, too large, or over the page cap
    CONNECTION_ERROR = "connection_error"


# =============================================================================
# Scoring
# =============================================================================
class SeoCategory(StrEnum):
    """The eight weighted SEO categories (spec section 6)."""

    METADATA = "metadata"
    TECHNICAL = "technical"
    CONTENT = "content"
    STRUCTURE = "structure"
    LINKS = "links"
    IMAGES = "images"
    SCHEMA = "schema"
    READABILITY = "readability"


class AiVisibilityCategory(StrEnum):
    """AI-search readiness signal groups (spec section 7)."""

    ANSWERABILITY = "answerability"  # FAQ / Q&A formatting
    STRUCTURE = "structure"  # headings, semantic organisation
    SCHEMA = "schema"  # machine-readable markup
    ENTITY_CLARITY = "entity_clarity"  # who/what/where is unambiguous
    COMPLETENESS = "completeness"  # topical depth and coverage
    CITATION_READINESS = "citation_readiness"  # sources, authorship, evidence
    FRESHNESS = "freshness"  # detectable recency signals
    TRUST = "trust"  # business info, contact, author bio


class ScoreGrade(StrEnum):
    """Bucketed score band, used for colour and copy in the UI."""

    EXCELLENT = "excellent"  # 90-100
    GOOD = "good"  # 75-89
    FAIR = "fair"  # 60-74
    POOR = "poor"  # 40-59
    CRITICAL = "critical"  # 0-39

    @classmethod
    def from_score(cls, score: float) -> ScoreGrade:
        if score >= 90:
            return cls.EXCELLENT
        if score >= 75:
            return cls.GOOD
        if score >= 60:
            return cls.FAIR
        if score >= 40:
            return cls.POOR
        return cls.CRITICAL


# =============================================================================
# Issues and recommendations
# =============================================================================
class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"

    @property
    def rank(self) -> int:
        """Sort weight; higher is more urgent."""
        return {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}[self.value]


class ImpactLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def weight(self) -> float:
        return {"high": 3.0, "medium": 2.0, "low": 1.0}[self.value]


class EffortLevel(StrEnum):
    """How much work applying a recommendation takes."""

    TRIVIAL = "trivial"  # a single field, minutes
    LOW = "low"  # one page, under an hour
    MEDIUM = "medium"  # a few hours
    HIGH = "high"  # multi-day or ongoing

    @property
    def weight(self) -> float:
        return {"trivial": 1.0, "low": 1.5, "medium": 3.0, "high": 5.0}[self.value]


class RecommendationStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class RecommendationCategory(StrEnum):
    METADATA = "metadata"
    TECHNICAL = "technical"
    CONTENT = "content"
    STRUCTURE = "structure"
    LINKS = "links"
    IMAGES = "images"
    SCHEMA = "schema"
    READABILITY = "readability"
    AI_VISIBILITY = "ai_visibility"
    COMPETITIVE = "competitive"


class PriorityBucket(StrEnum):
    """Presentation grouping derived from impact vs effort (spec section 21)."""

    QUICK_WIN = "quick_win"  # high impact, low effort
    MAJOR_PROJECT = "major_project"  # high impact, high effort
    FILL_IN = "fill_in"  # low impact, low effort
    LONG_TERM = "long_term"  # low impact, high effort


# =============================================================================
# Content
# =============================================================================
class ContentType(StrEnum):
    BLOG_ARTICLE = "blog_article"
    FAQ = "faq"
    META_TITLE = "meta_title"
    META_DESCRIPTION = "meta_description"
    PRODUCT_DESCRIPTION = "product_description"
    LINKEDIN_POST = "linkedin_post"
    INSTAGRAM_CAPTION = "instagram_caption"
    REDDIT_POST = "reddit_post"
    FACEBOOK_POST = "facebook_post"
    MARKETING_EMAIL = "marketing_email"
    CTA = "cta"
    COMPARISON_ARTICLE = "comparison_article"
    LANDING_COPY = "landing_copy"

    @property
    def is_long_form(self) -> bool:
        return self in {
            ContentType.BLOG_ARTICLE,
            ContentType.COMPARISON_ARTICLE,
            ContentType.FAQ,
        }

    @property
    def is_social(self) -> bool:
        return self in {
            ContentType.LINKEDIN_POST,
            ContentType.INSTAGRAM_CAPTION,
            ContentType.REDDIT_POST,
            ContentType.FACEBOOK_POST,
        }


class ContentStatus(StrEnum):
    DRAFT = "draft"
    GENERATING = "generating"
    READY = "ready"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class ContentSource(StrEnum):
    """Provenance of a piece of content. Never inferred, always recorded."""

    AI_GENERATED = "ai_generated"
    AI_ASSISTED = "ai_assisted"  # generated then edited by the user
    USER_WRITTEN = "user_written"


class ContentOpportunityStatus(StrEnum):
    SUGGESTED = "suggested"
    ACCEPTED = "accepted"  # turned into a content draft
    DISMISSED = "dismissed"


# =============================================================================
# Publishing
# =============================================================================
class Platform(StrEnum):
    WORDPRESS = "wordpress"
    LINKEDIN = "linkedin"
    REDDIT = "reddit"
    # Architecturally supported, no live connector yet (spec section 44).
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    X = "x"
    PINTEREST = "pinterest"
    MEDIUM = "medium"

    @property
    def has_live_connector(self) -> bool:
        """Whether a real API implementation exists for this platform.

        Distinct from *configured*: a platform can have a live connector
        implementation but no credentials supplied, in which case it runs in
        demo mode.
        """
        return self in {Platform.WORDPRESS, Platform.LINKEDIN, Platform.REDDIT}


class ConnectionMode(StrEnum):
    """Whether a stored connection talks to a real API or simulates it.

    This value is persisted on every connection, post, and analytics row, and is
    surfaced in the UI. It is the mechanism that keeps spec section 46 honest:
    simulated results are structurally impossible to mistake for real ones.
    """

    LIVE = "live"
    DEMO = "demo"


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    EXPIRED = "expired"  # refresh token no longer valid
    REVOKED = "revoked"  # user disconnected at the platform
    ERROR = "error"
    DISCONNECTED = "disconnected"


class PostStatus(StrEnum):
    """Lifecycle of a publish attempt (spec section 15)."""

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            PostStatus.PUBLISHED,
            PostStatus.CANCELLED,
        }


# =============================================================================
# Analytics
# =============================================================================
class MetricSource(StrEnum):
    """Provenance of every analytics datapoint (spec section 25).

    Required on every row. The dashboard renders a distinct badge per source, so
    a simulated number can never be displayed as a real one.
    """

    PLATFORM_API = "platform_api"  # fetched from a real platform API
    DEMO = "demo"  # generated by a demo connector
    USER_ENTERED = "user_entered"  # typed in by the user
    DERIVED = "derived"  # computed by AURA from other real metrics


class MetricKind(StrEnum):
    REACH = "reach"
    IMPRESSIONS = "impressions"
    ENGAGEMENT = "engagement"
    LIKES = "likes"
    COMMENTS = "comments"
    SHARES = "shares"
    CLICKS = "clicks"
    SAVES = "saves"
    VIDEO_VIEWS = "video_views"
    WEBSITE_SESSIONS = "website_sessions"
    WEBSITE_USERS = "website_users"


# =============================================================================
# Assistant
# =============================================================================
class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


# =============================================================================
# System
# =============================================================================
class NotificationType(StrEnum):
    SCAN_COMPLETED = "scan_completed"
    SCAN_FAILED = "scan_failed"
    CONTENT_GENERATED = "content_generated"
    PUBLISH_SUCCEEDED = "publish_succeeded"
    PUBLISH_FAILED = "publish_failed"
    SCHEDULE_UPCOMING = "schedule_upcoming"
    NEW_RECOMMENDATION = "new_recommendation"
    SCORE_IMPROVED = "score_improved"
    SCORE_DECLINED = "score_declined"
    ANALYTICS_UPDATED = "analytics_updated"


class NotificationLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class ActivityAction(StrEnum):
    USER_REGISTERED = "user_registered"
    USER_LOGGED_IN = "user_logged_in"
    USER_LOGGED_OUT = "user_logged_out"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_CHANGED = "password_changed"
    BUSINESS_CREATED = "business_created"
    BUSINESS_UPDATED = "business_updated"
    ONBOARDING_COMPLETED = "onboarding_completed"
    WEBSITE_ADDED = "website_added"
    WEBSITE_ANALYZED = "website_analyzed"
    SCORE_CHANGED = "score_changed"
    RECOMMENDATION_STATUS_CHANGED = "recommendation_status_changed"
    CONTENT_GENERATED = "content_generated"
    CONTENT_UPDATED = "content_updated"
    CONTENT_OPTIMIZED = "content_optimized"
    CONTENT_DELETED = "content_deleted"
    CONTENT_SCHEDULED = "content_scheduled"
    CONTENT_PUBLISHED = "content_published"
    PLATFORM_CONNECTED = "platform_connected"
    PLATFORM_DISCONNECTED = "platform_disconnected"
    ASSISTANT_QUERIED = "assistant_queried"


class ThemePreference(StrEnum):
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"
