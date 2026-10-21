"""Shared FastAPI dependencies: database session and the authenticated user.

The bearer-token flow is deliberately split into three dependencies so routes can
ask for exactly the level of trust they need:

* :func:`get_current_claims`  -- token verified, nothing loaded from the database.
* :func:`get_current_user`    -- claims resolved to a live ``users`` row.
* :func:`get_optional_user`   -- same, but ``None`` instead of a 401.

Every one of them fails closed: any problem raises
:class:`~app.core.errors.AuthenticationError` rather than returning ``None`` into
code that would then treat the request as anonymous-but-authorised.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.base import AuthBackend, AuthClaims, RequestContext
from app.auth.registry import get_auth_backend
from app.core.database import get_db
from app.core.errors import AuthenticationError
from app.models.business import Business
from app.models.user import User
from app.services.business import get_owned_business

# auto_error=False so a missing header becomes our own 401 envelope rather than
# FastAPI's bare {"detail": "Not authenticated"}.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="Bearer")

DbSession = Annotated[Session, Depends(get_db)]


def get_backend() -> AuthBackend:
    return get_auth_backend()


AuthProvider = Annotated[AuthBackend, Depends(get_backend)]


def get_request_context(request: Request) -> RequestContext:
    """Client metadata for the session/device list.

    ``X-Forwarded-For`` is honoured because the target deployments (Render, Fly,
    any reverse proxy) terminate TLS upstream, so ``request.client`` would
    otherwise be the proxy for every user. Only the first hop is taken, and the
    value is treated as a display label -- never as an authorisation input, since
    the header is trivially spoofable.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else None
    if not ip and request.client is not None:
        ip = request.client.host

    user_agent = request.headers.get("user-agent")
    if user_agent and len(user_agent) > 300:
        user_agent = user_agent[:300]

    return RequestContext(ip_address=ip or None, user_agent=user_agent or None)


RequestMeta = Annotated[RequestContext, Depends(get_request_context)]


def get_current_claims(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    backend: AuthProvider,
) -> AuthClaims:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError(
            "Please sign in to continue.",
            code="not_authenticated",
            hint="Send an Authorization: Bearer <token> header.",
        )
    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError(
            "Unsupported authorization scheme.", code="invalid_auth_scheme"
        )
    return backend.verify_token(credentials.credentials)


CurrentClaims = Annotated[AuthClaims, Depends(get_current_claims)]


def get_current_user(
    claims: CurrentClaims, backend: AuthProvider, session: DbSession
) -> User:
    """The authenticated account.

    Under the local provider this also re-checks that the session behind the
    token has not been revoked, which is what makes signing out take effect
    immediately instead of at token expiry.
    """
    return backend.resolve_user(session, claims)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ],
    backend: AuthProvider,
    session: DbSession,
) -> User | None:
    """For endpoints that personalise when signed in but work when not.

    An *invalid* token is still rejected: silently treating a bad token as
    anonymous would hide expiry bugs from the client.
    """
    if credentials is None or not credentials.credentials:
        return None
    claims = backend.verify_token(credentials.credentials)
    return backend.resolve_user(session, claims)


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def get_path_business(
    business_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> Business:
    """Resolve ``{business_id}`` from the path, scoped to the caller.

    This is the tenant boundary as a dependency. Declaring ``OwnedBusiness`` on a
    route is what makes another account's data unreachable from it -- the lookup
    filters on ``owner_id`` in SQL, so there is no version of the route that
    forgets to check. Every business-scoped resource (profile, websites, and later
    scans, content, and connections) hangs off the object this returns.
    """
    return get_owned_business(session, user, business_id)


OwnedBusiness = Annotated[Business, Depends(get_path_business)]


def db_session() -> Generator[Session]:
    """Re-exported for scripts that want the same session semantics as requests."""
    yield from get_db()


__all__ = [
    "AuthProvider",
    "CurrentClaims",
    "CurrentUser",
    "DbSession",
    "OptionalUser",
    "OwnedBusiness",
    "RequestMeta",
    "get_backend",
    "get_current_claims",
    "get_current_user",
    "get_optional_user",
    "get_path_business",
    "get_request_context",
]
