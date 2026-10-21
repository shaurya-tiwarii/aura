"""Website, crawled pages, and scan runs (Modules 4 and 5)."""

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
from app.models.enums import MonitorFrequency, PageFetchStatus, ScanStatus, ScanTrigger

if TYPE_CHECKING:
    from app.models.ai_visibility import AiVisibilityReport
    from app.models.business import Business
    from app.models.seo import SeoReport


class Website(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A site registered for analysis.

    ``normalized_url`` and ``registrable_domain`` are stored, not derived on
    read, because they are the crawler's same-domain check and the deduplication
    key. Computing them once at write time keeps that decision in one place and
    makes the uniqueness constraint meaningful.
    """

    __tablename__ = "websites"

    business_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Scheme + host + path with tracking params and fragments stripped.
    normalized_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # Registrable domain via the public suffix list, e.g. "example.co.uk".
    # The crawler refuses to leave this domain.
    registrable_domain: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # --- cached site-level facts, refreshed each scan ----------------------
    robots_txt_found: Mapped[bool | None] = mapped_column(Boolean)
    sitemap_found: Mapped[bool | None] = mapped_column(Boolean)
    sitemap_url: Mapped[str | None] = mapped_column(String(2048))
    sitemap_url_count: Mapped[int | None] = mapped_column(Integer)
    detected_cms: Mapped[str | None] = mapped_column(String(80))

    last_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- continuous monitoring --------------------------------------------
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    monitor_frequency: Mapped[str] = mapped_column(
        String(16), nullable=False, default=MonitorFrequency.WEEKLY.value
    )
    monitor_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    monitor_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monitor_day_of_week: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    monitor_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    monitor_job_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    last_monitor_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    business: Mapped[Business] = relationship(back_populates="websites")
    scans: Mapped[list[WebsiteScan]] = relationship(
        back_populates="website",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="desc(WebsiteScan.created_at)",
    )

    __table_args__ = (
        UniqueConstraint(
            "business_id", "normalized_url", name="unique_website_per_business"
        ),
        Index("ix_websites_business_primary", "business_id", "is_primary"),
        Index("ix_websites_monitoring", "monitoring_enabled", "monitor_frequency"),
        CheckConstraint(
            f"monitor_frequency IN {MonitorFrequency.sql_in_list()}",
            name="valid_monitor_frequency",
        ),
        CheckConstraint("monitor_hour >= 0 AND monitor_hour <= 23", name="valid_monitor_hour"),
        CheckConstraint("monitor_minute >= 0 AND monitor_minute <= 59", name="valid_monitor_minute"),
        CheckConstraint("monitor_day_of_week >= 0 AND monitor_day_of_week <= 6", name="valid_monitor_day_of_week"),
    )


class WebsiteScan(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One crawl-and-analyse run.

    Scans are immutable once complete and are the unit the improvement loop
    diffs against: comparing two scans of the same site is how a score delta is
    attributed to applied recommendations (Module 20). Nothing overwrites a
    previous scan, which is what makes score history real rather than a
    single mutable "current score" row.
    """

    __tablename__ = "website_scans"

    website_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("websites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScanStatus.QUEUED.value, index=True
    )
    trigger: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ScanTrigger.MANUAL.value
    )
    # Monotonic per website: scan 1, 2, 3... Referenced in the UI as
    # "Scan #3" and used to order improvement history.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # --- progress, polled by the frontend for staged loading states ---------
    # (spec section 27: "Checking website... / Extracting metadata... / ...")
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_message: Mapped[str | None] = mapped_column(String(200))

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    # --- crawl outcome ------------------------------------------------------
    pages_discovered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_crawled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pages_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # True when the page cap stopped the crawl early; shown to the user so a
    # partial score is never mistaken for a whole-site score.
    hit_page_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    robots_disallowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Structured failure info. ``error_code`` maps to an AuraError code so the
    # UI can show the same friendly message it would have shown live.
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)

    # The effective limits this scan ran under, recorded so an old scan remains
    # interpretable after the configured limits change.
    crawl_config: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Whole-site aggregates that are not page-specific: link graph summary,
    # duplicate-title clusters, sitemap coverage.
    site_analysis: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )

    website: Mapped[Website] = relationship(back_populates="scans")
    pages: Mapped[list[WebsitePage]] = relationship(
        back_populates="scan",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    seo_report: Mapped[SeoReport | None] = relationship(
        back_populates="scan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    ai_visibility_report: Mapped[AiVisibilityReport | None] = relationship(
        back_populates="scan",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (
        UniqueConstraint("website_id", "sequence", name="unique_scan_sequence"),
        Index("ix_website_scans_website_status", "website_id", "status"),
        Index("ix_website_scans_website_created", "website_id", "created_at"),
        CheckConstraint(
            "progress_percent >= 0 AND progress_percent <= 100",
            name="valid_progress",
        ),
    )

    @property
    def is_finished(self) -> bool:
        return self.status in {
            ScanStatus.COMPLETED.value,
            ScanStatus.FAILED.value,
            ScanStatus.PARTIAL.value,
        }


class WebsitePage(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single crawled page and everything extracted from it.

    Frequently-scored signals are real columns so the scoring engine can filter
    and aggregate in SQL (``COUNT(*) WHERE title IS NULL``). The bulkier
    extraction output - full heading tree, per-image records, parsed schema
    blocks - lives in JSON columns because it is only ever read for one page at
    a time when rendering the page detail view.
    """

    __tablename__ = "website_pages"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("website_scans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    normalized_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_homepage: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- fetch outcome ------------------------------------------------------
    fetch_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=PageFetchStatus.OK.value, index=True
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    final_url: Mapped[str | None] = mapped_column(String(2048))
    redirect_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_time_ms: Mapped[int | None] = mapped_column(Integer)
    content_bytes: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(120))
    fetch_error: Mapped[str | None] = mapped_column(Text)

    # --- metadata -----------------------------------------------------------
    title: Mapped[str | None] = mapped_column(String(1024))
    title_length: Mapped[int | None] = mapped_column(Integer)
    meta_description: Mapped[str | None] = mapped_column(Text)
    meta_description_length: Mapped[int | None] = mapped_column(Integer)
    canonical_url: Mapped[str | None] = mapped_column(String(2048))
    meta_robots: Mapped[str | None] = mapped_column(String(200))
    lang: Mapped[str | None] = mapped_column(String(20))
    has_open_graph: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    has_twitter_card: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    has_viewport_meta: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # --- structure ----------------------------------------------------------
    h1_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    h2_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    h3_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # True when levels are skipped (h1 -> h3) or an h1 is missing/duplicated.
    heading_hierarchy_valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # Ordered [{"level": 1, "text": "..."}] for the page detail outline view.
    heading_outline: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    # --- content ------------------------------------------------------------
    word_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paragraph_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Flesch Reading Ease (0-100, higher is easier). Nullable because it is
    # meaningless below a minimum word count.
    readability_score: Mapped[float | None] = mapped_column(Float)
    reading_grade_level: Mapped[float | None] = mapped_column(Float)
    text_to_html_ratio: Mapped[float | None] = mapped_column(Float)
    # Detected from <time>, article:published_time, or a visible date.
    published_date: Mapped[datetime | None] = mapped_column(UTCDateTime)
    modified_date: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # --- AI-visibility signals ---------------------------------------------
    has_faq_section: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    faq_question_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    has_list_content: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    has_table_content: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    has_author_info: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    has_contact_info: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    outbound_citation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # --- images -------------------------------------------------------------
    image_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    images_missing_alt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- links --------------------------------------------------------------
    internal_link_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_link_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Only counts links actually verified this scan; see ``links`` for detail.
    broken_link_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- schema -------------------------------------------------------------
    has_schema_markup: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # e.g. ["Organization", "FAQPage", "BreadcrumbList"]
    schema_types: Mapped[list[str]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )

    # --- bulky extraction detail -------------------------------------------
    images: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    links: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    schema_blocks: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONColumn, nullable=False, default=list
    )
    # Keyword -> occurrence count, for the target keywords on this business.
    keyword_usage: Mapped[dict[str, Any]] = mapped_column(
        JSONColumn, nullable=False, default=dict
    )
    # Extracted visible text, capped. Feeds AI content grounding and the
    # completeness heuristic. Not the full HTML, which is never stored.
    text_excerpt: Mapped[str | None] = mapped_column(Text)

    scan: Mapped[WebsiteScan] = relationship(back_populates="pages")

    __table_args__ = (
        UniqueConstraint("scan_id", "normalized_url", name="unique_page_per_scan"),
        Index("ix_website_pages_scan_status", "scan_id", "fetch_status"),
        Index("ix_website_pages_scan_homepage", "scan_id", "is_homepage"),
        CheckConstraint("depth >= 0", name="non_negative_depth"),
        CheckConstraint("word_count >= 0", name="non_negative_word_count"),
        CheckConstraint(
            "images_missing_alt <= image_count", name="alt_count_within_images"
        ),
    )

    @property
    def was_fetched(self) -> bool:
        """Whether this page yielded analysable HTML.

        Pages that failed to fetch are kept as rows (so the user can see *what*
        failed) but must be excluded from score averages.
        """
        return self.fetch_status in {
            PageFetchStatus.OK.value,
            PageFetchStatus.REDIRECTED.value,
        }
