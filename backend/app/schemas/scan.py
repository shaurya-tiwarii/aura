"""Response schemas for scans and pages.

Two-tier, matching the rest of the API: a summary model for lists (cheap, no
bulky JSON) and a ``*Detail`` subclass that adds the heavy per-page evidence for
a single-object fetch. Everything here is read-only -- scans are created by
hitting an endpoint, never by posting a body, because the only inputs a scan
takes (which website, which keywords) come from the path and the stored profile,
not the client.

Every field is a plain projection of a column the crawler computed and the scan
service stored, so the numbers a client sees are exactly the numbers the honesty
verifier reproduces from the same rows -- there is no massaging in this layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ============================================================================
# Scans
# ============================================================================


class ScanOut(BaseModel):
    """A scan as it appears in a list or a status poll.

    Carries the live progress fields (``status``, ``progress_percent``,
    ``progress_message``) so a single polled endpoint drives the whole
    queued -> crawling -> done UI, plus the terminal counts and any error. The
    denominators (``pages_discovered`` vs ``pages_crawled`` vs ``pages_failed``)
    are all present together on purpose: a "crawled" number is only meaningful
    next to how many were found and how many failed (§24).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    website_id: uuid.UUID
    status: str
    trigger: str
    sequence: int

    progress_percent: int
    progress_message: str | None
    is_finished: bool

    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None

    pages_discovered: int
    pages_crawled: int
    pages_failed: int
    hit_page_limit: bool
    robots_disallowed: bool

    error_code: str | None
    error_message: str | None

    created_at: datetime
    updated_at: datetime


class ScanDetailOut(ScanOut):
    """A single scan, with the crawl configuration and the site-level rollup.

    ``crawl_config`` is the exact set of limits the crawl ran under (so a result
    is reproducible), and ``site_analysis`` is :meth:`CrawlResult.site_summary`
    verbatim -- the aggregate counts later scoring stages read.
    """

    crawl_config: dict[str, Any] = Field(default_factory=dict)
    site_analysis: dict[str, Any] = Field(default_factory=dict)


# ============================================================================
# Pages
# ============================================================================


class WebsitePageOut(BaseModel):
    """One crawled page: how the fetch went, plus the scalar analysis signals.

    Excludes the bulky JSON columns (per-link lists, raw schema blocks, keyword
    tables) -- those are only in :class:`WebsitePageDetailOut` so a scan with 25
    pages does not ship 25 link graphs in a list response.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scan_id: uuid.UUID
    url: str
    normalized_url: str
    depth: int
    is_homepage: bool

    # Fetch outcome
    fetch_status: str
    was_fetched: bool
    http_status: int | None
    final_url: str | None
    redirect_count: int
    response_time_ms: int | None
    content_bytes: int | None
    content_type: str | None
    fetch_error: str | None

    # Metadata / head
    title: str | None
    title_length: int | None
    meta_description: str | None
    meta_description_length: int | None
    canonical_url: str | None
    meta_robots: str | None
    lang: str | None
    has_open_graph: bool
    has_twitter_card: bool
    has_viewport_meta: bool

    # Structure
    h1_count: int
    h2_count: int
    h3_count: int
    heading_hierarchy_valid: bool

    # Content
    word_count: int
    paragraph_count: int
    readability_score: float | None
    reading_grade_level: float | None
    text_to_html_ratio: float | None
    published_date: datetime | None
    modified_date: datetime | None

    # AI-visibility-relevant signals
    has_faq_section: bool
    faq_question_count: int
    has_list_content: bool
    has_table_content: bool
    has_author_info: bool
    has_contact_info: bool
    outbound_citation_count: int

    # Media & links
    image_count: int
    images_missing_alt: int
    internal_link_count: int
    external_link_count: int
    broken_link_count: int

    # Structured data
    has_schema_markup: bool
    schema_types: list[str]

    created_at: datetime
    updated_at: datetime


class WebsitePageDetailOut(WebsitePageOut):
    """A single page with the full evidence attached.

    These are the columns that make a later score *explainable* -- the actual
    heading outline, the resolved links with their status, the raw JSON-LD blocks,
    and where each keyword appeared -- so the detail view can show why a page
    scored the way it did rather than just the number.
    """

    heading_outline: list[dict[str, Any]] = Field(default_factory=list)
    images: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    schema_blocks: list[dict[str, Any]] = Field(default_factory=list)
    keyword_usage: dict[str, Any] = Field(default_factory=dict)
    text_excerpt: str | None


__all__ = [
    "ScanDetailOut",
    "ScanOut",
    "WebsitePageDetailOut",
    "WebsitePageOut",
]
