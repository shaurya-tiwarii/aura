"""Authentication endpoints.

Every route delegates to the configured :class:`~app.auth.base.AuthBackend`, so
this module contains no provider-specific logic. Operations a provider cannot
perform raise ``auth_operation_unsupported`` with a message naming what to do
instead -- the frontend reads ``GET /auth/capabilities`` and does not render
those controls in the first place.

Commit boundaries are explicit rather than relying on a middleware-level commit:
an auth mutation that half-applies (password changed, sessions not revoked) is
worse than one that fails outright.
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Request, Response, status

from app.api.deps import (
    AuthProvider,
    CurrentClaims,
    CurrentUser,
    DbSession,
    RequestMeta,
)
from app.auth.base import TokenPair
from app.core.config import settings
from app.core.logging import get_logger
from app.core.rate_limit import AUTH_LIMIT, limit_route
from app.models.user import User
from app.schemas.auth import (
    AuthSessionOut,
    CapabilitiesOut,
    ChangePasswordRequest,
    ForgotPasswordOut,
    ForgotPasswordRequest,
    LoginRequest,
    MessageOut,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    SessionOut,
    TokenOut,
    UserOut,
)

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _session_payload(user: User, pair: TokenPair) -> AuthSessionOut:
    return AuthSessionOut(
        user=UserOut.model_validate(user),
        tokens=TokenOut(
            access_token=pair.access_token,
            token_type=pair.token_type,
            expires_in=pair.expires_in,
            refresh_token=pair.refresh_token,
            refresh_expires_in=pair.refresh_expires_in,
        ),
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesOut,
    summary="Which auth operations this installation supports",
)
def capabilities(backend: AuthProvider) -> CapabilitiesOut:
    """Drives the sign-in UI so it never offers an action the backend refuses."""
    return CapabilitiesOut(**backend.capabilities.as_dict())


@router.post(
    "/register",
    response_model=AuthSessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account (local provider only)",
)
@limit_route(AUTH_LIMIT)
def register(
    request: Request,
    payload: RegisterRequest,
    backend: AuthProvider,
    session: DbSession,
    context: RequestMeta,
) -> AuthSessionOut:
    user = backend.register(
        session,
        email=str(payload.email),
        password=payload.password,
        full_name=payload.full_name,
        context=context,
    )
    # Signing in immediately after registering saves a round trip and avoids a
    # window where the account exists but the user has no way into it.
    _, pair = backend.login(
        session, email=str(payload.email), password=payload.password, context=context
    )
    session.commit()
    session.refresh(user)
    return _session_payload(user, pair)


@router.post(
    "/login",
    response_model=AuthSessionOut,
    summary="Exchange email and password for tokens",
)
@limit_route(AUTH_LIMIT)
def login(
    request: Request,
    payload: LoginRequest,
    backend: AuthProvider,
    session: DbSession,
    context: RequestMeta,
) -> AuthSessionOut:
    user, pair = backend.login(
        session, email=str(payload.email), password=payload.password, context=context
    )
    session.commit()
    session.refresh(user)
    return _session_payload(user, pair)


@router.post(
    "/refresh",
    response_model=AuthSessionOut,
    summary="Rotate a refresh token for a new token pair",
)
@limit_route(AUTH_LIMIT)
def refresh(
    request: Request,
    payload: RefreshRequest,
    backend: AuthProvider,
    session: DbSession,
    context: RequestMeta,
) -> AuthSessionOut:
    """Refresh tokens are single-use.

    The old token is revoked as part of this call; presenting it again is treated
    as theft and ends every session in the family.
    """
    user, pair = backend.refresh(
        session, refresh_token=payload.refresh_token, context=context
    )
    session.commit()
    session.refresh(user)
    return _session_payload(user, pair)


@router.get("/me", response_model=UserOut, summary="The signed-in account")
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.post("/logout", response_model=MessageOut, summary="End the current session")
def logout(
    claims: CurrentClaims,
    backend: AuthProvider,
    session: DbSession,
) -> MessageOut:
    """Revokes this session server-side where the provider allows it.

    Under Supabase the backend has no authority over the session, so the response
    says so plainly instead of reporting a revocation that did not happen.
    """
    revoked = backend.logout(session, claims=claims)
    session.commit()
    if revoked:
        return MessageOut(
            status="signed_out",
            message="You have been signed out on this device.",
        )
    return MessageOut(
        status="client_signout_required",
        message=(
            "Sign-out for this provider is handled by the client. "
            "Discard the stored token and call the provider's sign-out method."
        ),
        detail={"provider": backend.name},
    )


@router.post(
    "/logout-all",
    response_model=MessageOut,
    summary="End every session for this account",
)
def logout_all(
    user: CurrentUser, backend: AuthProvider, session: DbSession
) -> MessageOut:
    count = backend.logout_all(session, user=user)
    session.commit()
    return MessageOut(
        status="signed_out_everywhere",
        message=f"Signed out of {count} session{'s' if count != 1 else ''}.",
        detail={"sessions_revoked": count},
    )


@router.get(
    "/sessions",
    response_model=list[SessionOut],
    summary="Active sessions for this account",
)
def list_sessions(
    user: CurrentUser,
    claims: CurrentClaims,
    backend: AuthProvider,
    session: DbSession,
) -> list[SessionOut]:
    """Real rows from ``user_sessions`` -- never a placeholder list.

    Providers that do not expose sessions to AURA return an empty list, and
    ``GET /auth/capabilities`` reports ``can_revoke_sessions: false`` so the UI
    can explain why rather than showing an empty table.
    """
    return [
        SessionOut(
            id=record.id,
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            expires_at=record.expires_at,
            ip_address=record.ip_address,
            user_agent=record.user_agent,
            is_current=(record.id == claims.session_id),
        )
        for record in backend.active_sessions(session, user=user)
    ]


@router.delete(
    "/sessions/{session_id}",
    response_model=MessageOut,
    summary="Revoke one session",
)
def revoke_session(
    session_id: uuid.UUID,
    user: CurrentUser,
    backend: AuthProvider,
    session: DbSession,
) -> MessageOut:
    """Signs one device out immediately.

    The backend owns both the ownership check and the deliberately ambiguous
    "not found" response.
    """
    backend.revoke_session(session, user=user, session_id=session_id)
    session.commit()
    return MessageOut(status="revoked", message="That session has been signed out.")


@router.post(
    "/password/change",
    response_model=MessageOut,
    summary="Change the password of the signed-in account",
)
def change_password(
    payload: ChangePasswordRequest,
    user: CurrentUser,
    claims: CurrentClaims,
    backend: AuthProvider,
    session: DbSession,
) -> MessageOut:
    revoked = backend.change_password(
        session,
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        # Keep the caller signed in; end everything else.
        keep_session_id=claims.session_id,
    )
    session.commit()
    return MessageOut(
        status="password_changed",
        message=(
            "Your password has been changed. "
            f"{revoked} other session{'s' if revoked != 1 else ''} signed out."
        ),
        detail={"other_sessions_revoked": revoked},
    )


@router.post(
    "/password/forgot",
    response_model=ForgotPasswordOut,
    summary="Request a password reset link",
)
@limit_route(AUTH_LIMIT)
def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    backend: AuthProvider,
    session: DbSession,
    context: RequestMeta,
) -> ForgotPasswordOut:
    """Always answers the same way, whether or not the address is registered.

    No SMTP transport is configured in this build, so nothing is emailed. Rather
    than pretend otherwise, ``delivery`` reports ``not_configured`` and -- outside
    production only -- the link is returned directly so the reset flow is
    genuinely usable.
    """
    raw_token = backend.request_password_reset(
        session, email=str(payload.email), context=context
    )
    session.commit()

    generic = (
        "If an account exists for that address, a password reset link has been "
        "created."
    )

    if settings.is_production:
        # Never return the link in production: it would be a full account
        # takeover for anyone who can guess an email address.
        return ForgotPasswordOut(
            status="accepted",
            message=(
                f"{generic} Email delivery is not configured on this "
                "installation, so the link could not be sent. Contact the "
                "administrator."
            ),
            delivery="not_configured",
            reset_url=None,
        )

    reset_url = None
    if raw_token:
        reset_url = (
            f"{settings.public_base_url}/reset-password?token={quote(raw_token)}"
        )
        log.info("password_reset_link_generated", delivery="not_configured")

    return ForgotPasswordOut(
        status="accepted",
        message=(
            f"{generic} Email delivery is not configured, so the link is "
            "returned here for development use."
        ),
        delivery="not_configured",
        reset_url=reset_url,
    )


@router.post(
    "/password/reset",
    response_model=MessageOut,
    summary="Set a new password using a reset token",
)
@limit_route(AUTH_LIMIT)
def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    backend: AuthProvider,
    session: DbSession,
    response: Response,
) -> MessageOut:
    backend.reset_password(
        session, token=payload.token, new_password=payload.new_password
    )
    session.commit()
    response.status_code = status.HTTP_200_OK
    return MessageOut(
        status="password_reset",
        message=(
            "Your password has been reset and all existing sessions were signed "
            "out. Please sign in with your new password."
        ),
    )
