"""Request and response schemas for businesses, profiles, and websites.

Three conventions carried over from :mod:`app.schemas.auth`:

* **One output shape per resource.** A field cannot leak from a route that forgot
  to narrow its response, because there is only one model to narrow.
* **Update models are all-optional and use ``exclude_unset``** at the call site,
  so ``PATCH {"industry": null}`` clears the industry while
  ``PATCH {"name": "x"}`` leaves it untouched. Those two cases are
  indistinguishable if a schema uses ``None`` to mean both "absent" and "clear".
* **Bounds mirror the database columns**, so an over-long value fails as a 422
  field error rather than as a driver-level truncation or an integrity error.

Text is stripped and collapsed to ``None`` when empty. Storing ``""`` alongside
``NULL`` would give two spellings of "not provided", and every downstream
consumer -- prompt assembly especially -- would have to test for both.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from app.models.enums import OnboardingStep
from app.utils.urls import MAX_URL_LENGTH

# Column widths from app.models.business / app.models.website.
NAME_MAX = 200
INDUSTRY_MAX = 120
LOCATION_MAX = 200
DESCRIPTION_MAX = 4000
VOICE_MAX = 120
READING_LEVEL_MAX = 40
CONTACT_EMAIL_MAX = 320
CONTACT_PHONE_MAX = 60
TEXT_BLOCK_MAX = 2000

# List caps. These are not arbitrary: every one of these lists is inlined into a
# Gemini prompt, so an unbounded list is an unbounded prompt (and an unbounded
# bill of tokens for a free-tier key). The caps are generous for a small business
# and small enough that a full profile still fits comfortably in one prompt.
LIST_MAX_ITEMS = 40
LIST_ITEM_MAX = 200
KEYWORDS_MAX_ITEMS = 100
SOCIAL_LINKS_MAX = 12

# A single account holding hundreds of businesses is either an error or abuse;
# the agency use case in the spec is a handful of clients, not a directory.
MAX_BUSINESSES_PER_USER = 25
MAX_WEBSITES_PER_BUSINESS = 10

# No ``min_length`` here, deliberately. ``_clean_list`` below is documented to trim
# entries and drop blanks, and a ``min_length=1`` on the item type would pre-empt
# it: Pydantic validates each item *before* the field validator runs, so a form
# that posts ["Sourdough", ""] -- which is exactly what a dynamic list of inputs
# does when the user adds a row and leaves it empty -- would 422 with
# "services.3: String should have at least 1 character" instead of quietly
# ignoring the unfilled row.
#
# Note this is the opposite choice from the scalar fields, and the difference is
# intentional: a blank *name* is a mistake worth reporting (BusinessUpdate raises
# on it), whereas a blank *row in a list* carries no information at all. The
# max_length still guards the column width.
ShortText = Annotated[str, Field(max_length=LIST_ITEM_MAX)]


def _clean(value: str | None, *, limit: int | None = None) -> str | None:
    """Trim, collapse internal whitespace runs, and map empty to ``None``."""
    if value is None:
        return None
    collapsed = " ".join(value.split())
    if not collapsed:
        return None
    return collapsed[:limit] if limit else collapsed


def _clean_list(values: list[str] | None, *, limit: int) -> list[str] | None:
    """Trim entries, drop blanks, de-duplicate case-insensitively, keep order.

    Order is preserved because the user chose it -- the first service listed is
    the headline service, and re-sorting would quietly change what the AI treats
    as most important.
    """
    if values is None:
        return None
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in values:
        item = _clean(raw, limit=LIST_ITEM_MAX)
        if item is None:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
        if len(cleaned) >= limit:
            break
    return cleaned


# ============================================================================
# Business
# ============================================================================
class BusinessCreate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    industry: str | None = Field(default=None, max_length=INDUSTRY_MAX)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)
    location: str | None = Field(default=None, max_length=LOCATION_MAX)
    # Optional here and normalised by the service, which also registers it as the
    # first website. Onboarding asks for it in the same step as the name, so
    # requiring a second request would put a half-created business on screen.
    primary_website_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    # Unset means "default if this is the account's first business", which the
    # service decides. Passing true explicitly promotes it over the current one.
    is_default: bool | None = None

    @field_validator("name", "industry", "location", "description")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        return _clean(value)

    @field_validator("name")
    @classmethod
    def _require_name(cls, value: str | None) -> str:
        if not value:
            raise ValueError("Business name cannot be blank.")
        return value


class BusinessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    industry: str | None = Field(default=None, max_length=INDUSTRY_MAX)
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)
    location: str | None = Field(default=None, max_length=LOCATION_MAX)
    primary_website_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)

    @field_validator("industry", "location", "description")
    @classmethod
    def _tidy(cls, value: str | None) -> str | None:
        return _clean(value)

    @field_validator("name")
    @classmethod
    def _tidy_name(cls, value: str | None) -> str | None:
        cleaned = _clean(value)
        if value is not None and cleaned is None:
            # Distinct from "not sent": an explicit blank name is a mistake, and
            # silently ignoring it would look like the save succeeded.
            raise ValueError("Business name cannot be blank.")
        return cleaned

    @model_validator(mode="after")
    def _require_one_field(self) -> BusinessUpdate:
        if not self.model_fields_set:
            raise ValueError("Send at least one field to update.")
        return self


class OnboardingUpdate(BaseModel):
    """Advance or finish onboarding.

    The furthest-reached step is persisted rather than held in frontend state, so
    closing the tab mid-way resumes where the user stopped.
    """

    model_config = ConfigDict(extra="forbid")

    step: OnboardingStep | None = None
    # Explicit rather than inferred from ``step == COMPLETE``: finishing sets a
    # timestamp, and a timestamp should never be written as a side effect of a
    # string comparison.
    completed: bool | None = None

    @model_validator(mode="after")
    def _require_one_field(self) -> OnboardingUpdate:
        if self.step is None and self.completed is None:
            raise ValueError("Send a step, a completed flag, or both.")
        return self


class BusinessOut(BaseModel):
    """A business as the API reports it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    industry: str | None
    description: str | None
    location: str | None
    primary_website_url: str | None
    is_default: bool
    onboarding_step: str | None
    onboarding_completed_at: datetime | None
    # Read off the model property, so "is it onboarded" has one definition.
    is_onboarded: bool
    created_at: datetime
    updated_at: datetime


# ============================================================================
# Business profile
# ============================================================================
class BusinessProfileUpdate(BaseModel):
    """Partial update of the marketing profile.

    Every field is optional, and ``exclude_unset`` at the call site means an
    absent field is untouched while an explicit ``[]`` or ``null`` clears it.
    Onboarding fills this in over several steps, so a request that carried the
    whole profile would erase what a previous step had saved.
    """

    model_config = ConfigDict(extra="forbid")

    products: list[ShortText] | None = None
    services: list[ShortText] | None = None
    unique_selling_points: list[ShortText] | None = None

    target_audience: str | None = Field(default=None, max_length=TEXT_BLOCK_MAX)
    audience_segments: list[ShortText] | None = None
    service_areas: list[ShortText] | None = None

    brand_voice: str | None = Field(default=None, max_length=VOICE_MAX)
    brand_tone: str | None = Field(default=None, max_length=VOICE_MAX)
    content_restrictions: list[ShortText] | None = None
    preferred_reading_level: str | None = Field(
        default=None, max_length=READING_LEVEL_MAX
    )

    keywords: list[ShortText] | None = None
    social_links: dict[str, str] | None = None

    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=CONTACT_PHONE_MAX)
    # Upper bound is validated against the current year by the service, which is
    # the only place that should read a clock.
    founded_year: int | None = Field(default=None, ge=1000)

    @field_validator(
        "products",
        "services",
        "unique_selling_points",
        "audience_segments",
        "service_areas",
        "content_restrictions",
    )
    @classmethod
    def _tidy_list(cls, value: list[str] | None) -> list[str] | None:
        return _clean_list(value, limit=LIST_MAX_ITEMS)

    @field_validator("keywords")
    @classmethod
    def _tidy_keywords(cls, value: list[str] | None) -> list[str] | None:
        return _clean_list(value, limit=KEYWORDS_MAX_ITEMS)

    @field_validator(
        "target_audience",
        "brand_voice",
        "brand_tone",
        "preferred_reading_level",
        "contact_phone",
    )
    @classmethod
    def _tidy_text(cls, value: str | None) -> str | None:
        return _clean(value)

    @field_validator("social_links")
    @classmethod
    def _tidy_social(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        """Keys are platform slugs; values must be http(s) URLs.

        Validated here rather than trusted because these are rendered as links in
        the UI, and a ``javascript:`` value in an ``href`` is stored XSS.
        """
        if value is None:
            return None
        cleaned: dict[str, str] = {}
        for platform, url in list(value.items())[:SOCIAL_LINKS_MAX]:
            slug = (platform or "").strip().lower()
            link = (url or "").strip()
            if not slug or not link:
                continue
            if not slug.replace("_", "").replace("-", "").isalnum():
                raise ValueError(f"'{platform}' is not a valid platform name.")
            if len(slug) > 40 or len(link) > MAX_URL_LENGTH:
                raise ValueError(f"The link for '{slug}' is too long.")
            if not link.lower().startswith(("http://", "https://")):
                raise ValueError(
                    f"The link for '{slug}' must start with http:// or https://."
                )
            cleaned[slug] = link
        return cleaned

    @model_validator(mode="after")
    def _require_one_field(self) -> BusinessProfileUpdate:
        if not self.model_fields_set:
            raise ValueError("Send at least one field to update.")
        return self


class BusinessProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    business_id: uuid.UUID
    products: list[str]
    services: list[str]
    unique_selling_points: list[str]
    target_audience: str | None
    audience_segments: list[str]
    service_areas: list[str]
    brand_voice: str | None
    brand_tone: str | None
    content_restrictions: list[str]
    preferred_reading_level: str | None
    keywords: list[str]
    social_links: dict[str, Any]
    contact_email: str | None
    contact_phone: str | None
    founded_year: int | None
    created_at: datetime
    updated_at: datetime

    # Not stored: computed per response by the service. See
    # ``app.services.business.profile_completeness`` for the exact field list and
    # weighting -- it is a straight count of filled fields, not a quality score,
    # and the response says which fields are still empty so the number is
    # checkable rather than asserted.
    completeness_percent: int
    missing_fields: list[str]


# ============================================================================
# Website
# ============================================================================
class WebsiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    is_primary: bool | None = None


class WebsiteUpdate(BaseModel):
    """Only ``is_primary`` is editable.

    The URL is deliberately immutable. Scans, SEO reports, AI-visibility reports,
    and improvement deltas all hang off a website row, and every one of them is a
    measurement *of a specific address*. Editing the URL in place would silently
    re-attribute that history to a different site and make the improvement engine
    report a delta between two unrelated things. Adding the new address as its own
    website keeps both histories honest.
    """

    model_config = ConfigDict(extra="forbid")

    is_primary: bool


class WebsiteOut(BaseModel):
    """A registered website.

    The ``robots_txt_found`` / ``sitemap_*`` / ``detected_cms`` fields are cached
    crawl observations and are ``null`` until the first scan runs. Null means "not
    checked yet", never "absent" -- the UI must not render a missing sitemap
    warning off an unscanned site.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_id: uuid.UUID
    url: str
    normalized_url: str
    registrable_domain: str
    is_primary: bool
    robots_txt_found: bool | None
    sitemap_found: bool | None
    sitemap_url: str | None
    sitemap_url_count: int | None
    detected_cms: str | None
    last_scan_at: datetime | None
    monitoring_enabled: bool
    monitor_frequency: str
    monitor_hour: int
    monitor_minute: int
    monitor_day_of_week: int
    monitor_timezone: str
    monitor_job_id: str | None
    last_monitor_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BusinessDetailOut(BusinessOut):
    """One business with everything the dashboard needs to render it."""

    profile: BusinessProfileOut | None
    websites: list[WebsiteOut]


__all__ = [
    "MAX_BUSINESSES_PER_USER",
    "MAX_WEBSITES_PER_BUSINESS",
    "BusinessCreate",
    "BusinessDetailOut",
    "BusinessOut",
    "BusinessProfileOut",
    "BusinessProfileUpdate",
    "BusinessUpdate",
    "OnboardingUpdate",
    "WebsiteCreate",
    "WebsiteOut",
    "WebsiteUpdate",
]
