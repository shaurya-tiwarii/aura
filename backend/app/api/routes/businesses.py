"""Business, profile, and website endpoints (Modules 2 and 3).

Authorisation is structural rather than per-route: anything scoped to a business
takes the ``OwnedBusiness`` dependency, which resolves ``{business_id}`` with
``owner_id`` in the WHERE clause. A route cannot forget the check, because without
that dependency it has no business object to work with at all.

Routes own the commit. Creating a business can also create its first website and
demote a previous default, and those three writes are one unit -- a partial apply
would leave an account with two defaults or a business whose displayed URL is not
registered for it.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, OwnedBusiness
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.models.business import Business, BusinessProfile
from app.schemas.business import (
    BusinessCreate,
    BusinessDetailOut,
    BusinessOut,
    BusinessProfileOut,
    BusinessProfileUpdate,
    BusinessUpdate,
    OnboardingUpdate,
    WebsiteCreate,
    WebsiteOut,
    WebsiteUpdate,
)
from app.services import business as service

log = get_logger(__name__)

router = APIRouter(prefix="/businesses", tags=["businesses"])


def _detail(session: Session, business: Business) -> BusinessDetailOut:
    """Serialise a business with its profile and websites.

    Built explicitly rather than by ``from_attributes`` on the nested models,
    because the profile response carries two computed fields
    (``completeness_percent``, ``missing_fields``) that are not columns.
    """
    profile = service.get_profile(session, business)
    return BusinessDetailOut(
        **BusinessOut.model_validate(business).model_dump(),
        profile=_profile_out(profile) if profile is not None else None,
        websites=[
            WebsiteOut.model_validate(site)
            for site in service.list_websites(session, business)
        ],
    )


def _profile_out(profile: BusinessProfile) -> BusinessProfileOut:
    percent, missing = service.profile_completeness(profile)
    return BusinessProfileOut(
        **{
            field: getattr(profile, field)
            for field in BusinessProfileOut.model_fields
            if field not in {"completeness_percent", "missing_fields"}
        },
        completeness_percent=percent,
        missing_fields=missing,
    )


# ============================================================================
# Businesses
# ============================================================================
@router.get(
    "",
    response_model=list[BusinessOut],
    summary="List the businesses this account owns",
)
def list_businesses(user: CurrentUser, session: DbSession) -> list[BusinessOut]:
    return [
        BusinessOut.model_validate(item)
        for item in service.list_businesses(session, user)
    ]


@router.post(
    "",
    response_model=BusinessDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a business",
)
def create_business(
    payload: BusinessCreate, user: CurrentUser, session: DbSession
) -> BusinessDetailOut:
    """Create a business, and register ``primary_website_url`` if one was given.

    The URL is normalised and safety-checked before it is stored, so a blocked or
    malformed address fails the whole request rather than creating a business with
    an unusable website attached.
    """
    business, _ = service.create_business(session, user, payload)
    session.commit()
    session.refresh(business)
    return _detail(session, business)


@router.get(
    "/{business_id}",
    response_model=BusinessDetailOut,
    summary="One business, with its profile and websites",
)
def get_business(business: OwnedBusiness, session: DbSession) -> BusinessDetailOut:
    return _detail(session, business)


@router.patch(
    "/{business_id}",
    response_model=BusinessDetailOut,
    summary="Update a business",
)
def update_business(
    payload: BusinessUpdate, business: OwnedBusiness, session: DbSession
) -> BusinessDetailOut:
    service.update_business(session, business, payload)
    session.commit()
    session.refresh(business)
    return _detail(session, business)


@router.delete(
    "/{business_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a business and everything under it",
)
def delete_business(business: OwnedBusiness, session: DbSession) -> Response:
    """Irreversible: scans, reports, content, and connections all go with it.

    The cascade is declared in the database, not simulated here, so the same thing
    happens whatever deletes the row.
    """
    service.delete_business(session, business)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{business_id}/default",
    response_model=BusinessOut,
    summary="Make this the default business",
)
def set_default(business: OwnedBusiness, session: DbSession) -> BusinessOut:
    service.set_default_business(session, business)
    session.commit()
    session.refresh(business)
    return BusinessOut.model_validate(business)


@router.patch(
    "/{business_id}/onboarding",
    response_model=BusinessOut,
    summary="Record onboarding progress",
)
def update_onboarding(
    payload: OnboardingUpdate, business: OwnedBusiness, session: DbSession
) -> BusinessOut:
    service.update_onboarding(session, business, payload)
    session.commit()
    session.refresh(business)
    return BusinessOut.model_validate(business)


# ============================================================================
# Profile
# ============================================================================
@router.get(
    "/{business_id}/profile",
    response_model=BusinessProfileOut,
    summary="The marketing profile used to ground AI prompts",
)
def get_profile(business: OwnedBusiness, session: DbSession) -> BusinessProfileOut:
    """404 when onboarding has not created a profile yet.

    An empty profile is not invented for the response: "no profile" and "a profile
    with nothing in it" are different states, and the second one is a choice the
    user made.
    """
    profile = service.get_profile(session, business)
    if profile is None:
        raise NotFoundError(
            "This business does not have a profile yet.",
            hint="Send a PATCH to this endpoint to create one.",
        )
    return _profile_out(profile)


@router.patch(
    "/{business_id}/profile",
    response_model=BusinessProfileOut,
    summary="Create or update the marketing profile",
)
def upsert_profile(
    payload: BusinessProfileUpdate, business: OwnedBusiness, session: DbSession
) -> BusinessProfileOut:
    """Partial update: absent fields are untouched, explicit nulls clear.

    Creates the profile on first call, so the frontend never has to know whether
    one exists.
    """
    profile = service.upsert_profile(session, business, payload)
    session.commit()
    session.refresh(profile)
    return _profile_out(profile)


# ============================================================================
# Websites
# ============================================================================
@router.get(
    "/{business_id}/websites",
    response_model=list[WebsiteOut],
    summary="Websites registered for this business",
)
def list_websites(business: OwnedBusiness, session: DbSession) -> list[WebsiteOut]:
    return [
        WebsiteOut.model_validate(site)
        for site in service.list_websites(session, business)
    ]


@router.post(
    "/{business_id}/websites",
    response_model=WebsiteOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a website for analysis",
)
def add_website(
    payload: WebsiteCreate, business: OwnedBusiness, session: DbSession
) -> WebsiteOut:
    """The URL is canonicalised and SSRF-checked before it is stored.

    ``normalized_url`` in the response is the form AURA deduplicates by, so the UI
    can show the user exactly which address will be crawled -- typing
    ``Example.com/?utm_source=x`` and seeing ``https://example.com/`` back is the
    point.
    """
    website = service.add_website(session, business, payload)
    session.commit()
    session.refresh(website)
    return WebsiteOut.model_validate(website)


@router.get(
    "/{business_id}/websites/{website_id}",
    response_model=WebsiteOut,
    summary="One registered website",
)
def get_website(
    website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> WebsiteOut:
    return WebsiteOut.model_validate(
        service.get_owned_website(session, business, website_id)
    )


@router.patch(
    "/{business_id}/websites/{website_id}",
    response_model=WebsiteOut,
    summary="Change which website is the primary one",
)
def update_website(
    website_id: uuid.UUID,
    payload: WebsiteUpdate,
    business: OwnedBusiness,
    session: DbSession,
) -> WebsiteOut:
    """Only the primary flag is editable -- see :class:`WebsiteUpdate` for why."""
    website = service.get_owned_website(session, business, website_id)
    service.update_website(session, business, website, is_primary=payload.is_primary)
    session.commit()
    session.refresh(website)
    return WebsiteOut.model_validate(website)


@router.delete(
    "/{business_id}/websites/{website_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a website and its scan history",
)
def delete_website(
    website_id: uuid.UUID, business: OwnedBusiness, session: DbSession
) -> Response:
    website = service.get_owned_website(session, business, website_id)
    service.delete_website(session, business, website)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
