"""Business logic for businesses, their profile, and their websites.

**The tenant boundary lives here.** Every read and write goes through
:func:`get_owned_business`, which filters by ``owner_id`` in the SQL rather than
loading a row and comparing afterwards. Profiles and websites are only ever
reached *through* a business that already passed that filter, so there is no code
path to another account's data (spec section 20).

A missing row and someone else's row both raise the same 404. That is deliberate:
a 403 for an existing-but-not-yours id is an existence oracle -- it lets an
attacker enumerate which business ids are real. The auth module makes the same
choice for sessions, and the two need to agree or the inconsistency itself leaks
the answer.

Nothing here commits. Routes own the transaction boundary, because a request that
creates a business *and* its first website must not be able to half-apply.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.logging import get_logger
from app.models.business import Business, BusinessProfile
from app.models.enums import OnboardingStep
from app.models.user import User
from app.models.website import Website
from app.schemas.business import (
    MAX_BUSINESSES_PER_USER,
    MAX_WEBSITES_PER_BUSINESS,
    BusinessCreate,
    BusinessProfileUpdate,
    BusinessUpdate,
    OnboardingUpdate,
    WebsiteCreate,
)
from app.utils.urls import NormalizedURL, normalize_url

log = get_logger(__name__)

# Fields that make up a "complete" profile, in the order the onboarding form asks
# for them. Completeness is a plain count of filled fields out of these -- not a
# quality judgement -- and the API returns the unfilled names alongside the
# percentage so the number can be checked rather than taken on trust.
PROFILE_COMPLETENESS_FIELDS: tuple[str, ...] = (
    "products",
    "services",
    "unique_selling_points",
    "target_audience",
    "audience_segments",
    "service_areas",
    "brand_voice",
    "brand_tone",
    "keywords",
    "social_links",
    "contact_email",
)

# Onboarding order, used to keep ``onboarding_step`` monotonic.
ONBOARDING_ORDER: tuple[OnboardingStep, ...] = (
    OnboardingStep.BUSINESS_BASICS,
    OnboardingStep.WEBSITE,
    OnboardingStep.PROFILE,
    OnboardingStep.FIRST_SCAN,
    OnboardingStep.COMPLETE,
)

EARLIEST_FOUNDED_YEAR = 1000


def _now() -> datetime:
    return datetime.now(UTC)


# ============================================================================
# Businesses
# ============================================================================
def list_businesses(session: Session, owner: User) -> list[Business]:
    """Every business the account owns, default first, then alphabetical."""
    stmt = (
        select(Business)
        .where(Business.owner_id == owner.id)
        .order_by(Business.is_default.desc(), func.lower(Business.name))
    )
    return list(session.scalars(stmt))


def get_owned_business(
    session: Session,
    owner: User,
    business_id: uuid.UUID,
    *,
    with_children: bool = False,
) -> Business:
    """The one function every business-scoped request goes through.

    ``owner_id`` is part of the WHERE clause, not an assertion after the fact, so
    a forgotten check cannot silently return another tenant's row.
    """
    stmt = select(Business).where(
        Business.id == business_id, Business.owner_id == owner.id
    )
    if with_children:
        # Eager-load for the detail response: without this, serialising N websites
        # costs N queries, and the profile another.
        stmt = stmt.options(
            selectinload(Business.profile), selectinload(Business.websites)
        )
    business = session.scalars(stmt).first()
    if business is None:
        raise NotFoundError(
            "That business could not be found.",
            hint="It may have been deleted, or it belongs to another account.",
        )
    return business


def create_business(
    session: Session, owner: User, payload: BusinessCreate
) -> tuple[Business, Website | None]:
    """Create a business, optionally with its first website.

    Returns the business and the website that was created from
    ``primary_website_url`` (``None`` when no URL was given), so the route can
    report both without a second query.
    """
    existing = session.scalar(
        select(func.count()).select_from(Business).where(Business.owner_id == owner.id)
    )
    if (existing or 0) >= MAX_BUSINESSES_PER_USER:
        raise ConflictError(
            f"This account already has the maximum of {MAX_BUSINESSES_PER_USER} "
            "businesses.",
            code="business_limit_reached",
            hint="Delete one you no longer need, or use a separate account.",
        )

    _assert_name_available(session, owner_id=owner.id, name=payload.name)

    # The first business is always the default; later ones only if asked.
    is_default = payload.is_default if payload.is_default is not None else existing == 0

    business = Business(
        owner_id=owner.id,
        name=payload.name,
        industry=payload.industry,
        description=payload.description,
        location=payload.location,
        is_default=is_default,
        onboarding_step=OnboardingStep.BUSINESS_BASICS.value,
    )
    session.add(business)
    # Needed before the website row can reference business.id, and it also
    # surfaces the unique constraint as an IntegrityError here rather than at
    # commit time, where the route could no longer turn it into a clean 409.
    session.flush()

    website: Website | None = None
    if payload.primary_website_url:
        website = add_website(
            session,
            business,
            WebsiteCreate(url=payload.primary_website_url, is_primary=True),
        )
        # Mirrored onto the business for display and for the AI prompt context.
        # ``websites`` stays the source of truth for anything that crawls.
        business.primary_website_url = website.url
        business.onboarding_step = OnboardingStep.WEBSITE.value

    if is_default:
        _demote_other_defaults(session, owner_id=owner.id, keep_id=business.id)

    log.info(
        "business_created",
        business_id=str(business.id),
        has_website=website is not None,
    )
    return business, website


def update_business(
    session: Session, business: Business, payload: BusinessUpdate
) -> Business:
    """Apply a partial update.

    ``exclude_unset`` is what makes "clear this field" and "leave it alone"
    different requests instead of the same one.
    """
    data = payload.model_dump(exclude_unset=True)

    if "name" in data and data["name"] != business.name:
        _assert_name_available(
            session,
            owner_id=business.owner_id,
            name=data["name"],
            exclude_id=business.id,
        )

    if "primary_website_url" in data:
        raw = data.pop("primary_website_url")
        if raw is None:
            business.primary_website_url = None
        else:
            parsed = normalize_url(raw)
            business.primary_website_url = parsed.url
            # Keep the website list consistent with the field the user just
            # edited: if this address is not registered yet, register it. Leaving
            # them to diverge would mean the dashboard showed one URL while every
            # scan ran against another.
            _ensure_website_registered(session, business, parsed)

    for field, value in data.items():
        setattr(business, field, value)

    session.flush()
    return business


def set_default_business(session: Session, business: Business) -> Business:
    """Make this the business the dashboard opens to."""
    business.is_default = True
    _demote_other_defaults(session, owner_id=business.owner_id, keep_id=business.id)
    session.flush()
    return business


def delete_business(session: Session, business: Business) -> None:
    """Delete a business and everything under it.

    The cascade is enforced by ``ON DELETE CASCADE`` in the database as well as by
    the ORM relationships, so a raw SQL delete cannot leave orphaned scans behind.
    """
    was_default = business.is_default
    owner_id = business.owner_id
    session.delete(business)
    session.flush()

    if was_default:
        # Never leave an account with businesses but no default, or the dashboard
        # has nothing to open.
        replacement = session.scalars(
            select(Business)
            .where(Business.owner_id == owner_id)
            .order_by(Business.created_at)
            .limit(1)
        ).first()
        if replacement is not None:
            replacement.is_default = True
            session.flush()

    log.info("business_deleted", business_id=str(business.id))


def update_onboarding(
    session: Session, business: Business, payload: OnboardingUpdate
) -> Business:
    """Record onboarding progress.

    The step only ever moves forward. A user who revisits an earlier screen has
    not un-learned the later one, and letting the step go backwards would make the
    resume point wrong.
    """
    if payload.step is not None:
        current = _step_index(business.onboarding_step)
        proposed = ONBOARDING_ORDER.index(payload.step)
        if proposed > current:
            business.onboarding_step = payload.step.value

    if payload.completed is True:
        business.onboarding_step = OnboardingStep.COMPLETE.value
        # Idempotent: re-finishing keeps the original completion time, which is
        # what any "time to first value" reporting would need.
        if business.onboarding_completed_at is None:
            business.onboarding_completed_at = _now()
    elif payload.completed is False:
        business.onboarding_completed_at = None

    session.flush()
    return business


# ============================================================================
# Profile
# ============================================================================
def get_profile(session: Session, business: Business) -> BusinessProfile | None:
    """The stored profile, or ``None`` if onboarding has not created one yet."""
    return session.scalars(
        select(BusinessProfile).where(BusinessProfile.business_id == business.id)
    ).first()


def upsert_profile(
    session: Session, business: Business, payload: BusinessProfileUpdate
) -> BusinessProfile:
    """Create or partially update the profile.

    One endpoint rather than separate create/update routes: the frontend does not
    know or care whether a row exists yet, and making it find out first would add
    a round trip and a race.
    """
    data = payload.model_dump(exclude_unset=True)

    if "founded_year" in data and data["founded_year"] is not None:
        year = int(data["founded_year"])
        this_year = _now().year
        if not EARLIEST_FOUNDED_YEAR <= year <= this_year:
            raise ValidationFailedError(
                f"Founding year must be between {EARLIEST_FOUNDED_YEAR} and "
                f"{this_year}.",
                fields={"founded_year": "Not a plausible founding year."},
            )

    if "contact_email" in data and data["contact_email"] is not None:
        data["contact_email"] = str(data["contact_email"])

    profile = get_profile(session, business)
    if profile is None:
        profile = BusinessProfile(business_id=business.id)
        session.add(profile)

    for field, value in data.items():
        # A list/dict column is NOT NULL with a default of empty, so "clear it"
        # has to write the empty container rather than NULL -- otherwise every
        # consumer would need to handle both spellings of "nothing here".
        if value is None and field in _CONTAINER_FIELDS:
            value = [] if field != "social_links" else {}
        setattr(profile, field, value)

    session.flush()
    return profile


def profile_completeness(profile: BusinessProfile | None) -> tuple[int, list[str]]:
    """Percentage of profile fields filled, plus the names of the empty ones.

    Deliberately a plain count over :data:`PROFILE_COMPLETENESS_FIELDS`: it is a
    progress indicator, not a score, and returning the missing field names next to
    it keeps it checkable instead of an unexplained number (spec section 24).
    """
    if profile is None:
        return 0, list(PROFILE_COMPLETENESS_FIELDS)

    missing = [
        field
        for field in PROFILE_COMPLETENESS_FIELDS
        if not getattr(profile, field, None)
    ]
    filled = len(PROFILE_COMPLETENESS_FIELDS) - len(missing)
    percent = round(filled * 100 / len(PROFILE_COMPLETENESS_FIELDS))
    return percent, missing


_CONTAINER_FIELDS = frozenset(
    {
        "products",
        "services",
        "unique_selling_points",
        "audience_segments",
        "service_areas",
        "content_restrictions",
        "keywords",
        "social_links",
    }
)


# ============================================================================
# Websites
# ============================================================================
def list_websites(session: Session, business: Business) -> list[Website]:
    stmt = (
        select(Website)
        .where(Website.business_id == business.id)
        .order_by(Website.is_primary.desc(), Website.created_at)
    )
    return list(session.scalars(stmt))


def get_owned_website(
    session: Session, business: Business, website_id: uuid.UUID
) -> Website:
    """A website, scoped to a business the caller already owns.

    The ``business_id`` predicate is what keeps this from being an
    id-guessing hole: the business was resolved by owner, so filtering websites by
    that business is the second half of the same boundary.
    """
    website = session.scalars(
        select(Website).where(
            Website.id == website_id, Website.business_id == business.id
        )
    ).first()
    if website is None:
        raise NotFoundError(
            "That website could not be found.",
            hint="It may have been removed, or it belongs to another business.",
        )
    return website


def add_website(
    session: Session, business: Business, payload: WebsiteCreate
) -> Website:
    """Register a website after normalising and safety-checking its URL."""
    parsed = normalize_url(payload.url)

    count = session.scalar(
        select(func.count())
        .select_from(Website)
        .where(Website.business_id == business.id)
    )
    if (count or 0) >= MAX_WEBSITES_PER_BUSINESS:
        raise ConflictError(
            f"This business already has the maximum of "
            f"{MAX_WEBSITES_PER_BUSINESS} websites.",
            code="website_limit_reached",
        )

    duplicate = session.scalars(
        select(Website).where(
            Website.business_id == business.id,
            Website.normalized_url == parsed.normalized,
        )
    ).first()
    if duplicate is not None:
        raise ConflictError(
            "That website is already registered for this business.",
            code="website_already_registered",
            detail=duplicate.url,
        )

    is_primary = payload.is_primary if payload.is_primary is not None else count == 0

    website = Website(
        business_id=business.id,
        url=parsed.url,
        normalized_url=parsed.normalized,
        registrable_domain=parsed.registrable_domain or parsed.host,
        is_primary=is_primary,
    )
    session.add(website)

    try:
        session.flush()
    except IntegrityError as exc:
        # The unique constraint is the backstop for the pre-check above losing a
        # race between two concurrent requests. Rolling back to a savepoint is not
        # possible here, so this is reported as the conflict it is and the route's
        # failed transaction is discarded.
        raise ConflictError(
            "That website is already registered for this business.",
            code="website_already_registered",
        ) from exc

    if is_primary:
        _demote_other_primaries(session, business_id=business.id, keep_id=website.id)
        if not business.primary_website_url:
            business.primary_website_url = website.url

    log.info(
        "website_registered",
        business_id=str(business.id),
        website_id=str(website.id),
        domain=website.registrable_domain,
    )
    return website


def update_website(
    session: Session, business: Business, website: Website, *, is_primary: bool
) -> Website:
    """Promote or demote a website.

    Demoting the only website is refused rather than silently ignored: a business
    with websites but no primary has no defined scan target, and the caller should
    hear about that instead of discovering it at scan time.
    """
    if is_primary:
        website.is_primary = True
        _demote_other_primaries(session, business_id=business.id, keep_id=website.id)
        business.primary_website_url = website.url
    else:
        if website.is_primary:
            replacement = session.scalars(
                select(Website)
                .where(Website.business_id == business.id, Website.id != website.id)
                .order_by(Website.created_at)
                .limit(1)
            ).first()
            if replacement is None:
                raise ValidationFailedError(
                    "This is the only website for this business, so it has to stay "
                    "the primary one.",
                    code="primary_website_required",
                    hint="Add another website first, then change the primary.",
                )
            website.is_primary = False
            replacement.is_primary = True
            business.primary_website_url = replacement.url

    session.flush()
    return website


def delete_website(session: Session, business: Business, website: Website) -> None:
    """Remove a website and its scan history, then repair the primary flag."""
    was_primary = website.is_primary
    session.delete(website)
    session.flush()

    remaining = list_websites(session, business)
    if was_primary:
        if remaining:
            remaining[0].is_primary = True
            business.primary_website_url = remaining[0].url
        else:
            business.primary_website_url = None
        session.flush()

    log.info("website_deleted", business_id=str(business.id))


# ============================================================================
# Internals
# ============================================================================
def _assert_name_available(
    session: Session,
    *,
    owner_id: uuid.UUID,
    name: str,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Pre-check the per-owner name constraint to produce a clean 409.

    The database constraint remains the real guarantee; this exists so the common
    case is a helpful message rather than an IntegrityError. Comparison is
    case-insensitive because "Bakery" and "bakery" are the same business to a
    human, even though the constraint would allow both.
    """
    stmt = select(Business.id).where(
        Business.owner_id == owner_id, func.lower(Business.name) == name.lower()
    )
    if exclude_id is not None:
        stmt = stmt.where(Business.id != exclude_id)
    if session.scalars(stmt).first() is not None:
        raise ConflictError(
            f"You already have a business named '{name}'.",
            code="business_name_taken",
            fields={"name": "Already used by another of your businesses."},
        )


def _demote_other_defaults(
    session: Session, *, owner_id: uuid.UUID, keep_id: uuid.UUID
) -> None:
    for other in session.scalars(
        select(Business).where(
            Business.owner_id == owner_id,
            Business.id != keep_id,
            Business.is_default.is_(True),
        )
    ):
        other.is_default = False


def _demote_other_primaries(
    session: Session, *, business_id: uuid.UUID, keep_id: uuid.UUID
) -> None:
    for other in session.scalars(
        select(Website).where(
            Website.business_id == business_id,
            Website.id != keep_id,
            Website.is_primary.is_(True),
        )
    ):
        other.is_primary = False


def _ensure_website_registered(
    session: Session, business: Business, parsed: NormalizedURL
) -> Website:
    """Register ``parsed`` for the business if it is not already, and make it primary."""
    existing = session.scalars(
        select(Website).where(
            Website.business_id == business.id,
            Website.normalized_url == parsed.normalized,
        )
    ).first()
    if existing is not None:
        existing.is_primary = True
        _demote_other_primaries(session, business_id=business.id, keep_id=existing.id)
        session.flush()
        return existing
    return add_website(
        session, business, WebsiteCreate(url=parsed.url, is_primary=True)
    )


def _step_index(step: str | None) -> int:
    """Position of a stored step in the onboarding order; ``-1`` if unset.

    Tolerates an unrecognised value rather than raising: the CHECK constraint
    prevents one being written, but a schema predating the constraint should not
    make the endpoint unusable.
    """
    if not step:
        return -1
    try:
        return ONBOARDING_ORDER.index(OnboardingStep(step))
    except ValueError:
        return -1


__all__ = [
    "ONBOARDING_ORDER",
    "PROFILE_COMPLETENESS_FIELDS",
    "add_website",
    "create_business",
    "delete_business",
    "delete_website",
    "get_owned_business",
    "get_owned_website",
    "get_profile",
    "list_businesses",
    "list_websites",
    "profile_completeness",
    "set_default_business",
    "update_business",
    "update_onboarding",
    "update_website",
    "upsert_profile",
]
