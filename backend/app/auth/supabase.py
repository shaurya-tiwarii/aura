"""Supabase authentication provider: Supabase owns the credentials.

Active when ``AUTH_PROVIDER=supabase``. In this mode the browser talks to
Supabase directly (via ``supabase-js``) to sign up, sign in, refresh, and reset
passwords; AURA's backend never sees a password. Its job is narrower and
entirely real: **verify** the JWT on each request and mirror the identity into
the local ``users`` table so every other table has a stable foreign key.

That division is why most :class:`~app.auth.base.AuthBackend` operations are
unsupported here rather than reimplemented. Proxying sign-up through this backend
would need the Supabase *service role* key, which is an admin credential that
must never sit in an application server that also serves user traffic. Rather
than ship endpoints that appear to work and then fail, the capability flags say
plainly which operations belong to Supabase, and the API returns an actionable
error naming the client-side call to use instead.

Token verification uses the project's JWT secret (HS256), which is what
``SUPABASE_JWT_SECRET`` documents. Verification is local and offline -- no
network round trip per request, and no dependency on Supabase being reachable
for an already-issued token to be checked.

Newer Supabase projects sign user access tokens with asymmetric **JWT Signing
Keys** (ES256) instead of the legacy HS256 secret -- the dashboard's "JWT
Keys" page shows the legacy secret as verify-only. Those tokens are verified
against the project's public JWKS
(``{SUPABASE_URL}/auth/v1/.well-known/jwks.json``), selected by the token's
``kid`` header and additionally bound to the expected issuer
(``{SUPABASE_URL}/auth/v1``). The signing algorithm is read from the token
header, so legacy HS256 projects keep working unchanged.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.base import AuthBackend, AuthCapabilities, AuthClaims
from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.models.user import User

log = get_logger(__name__)

# Supabase signs project JWTs with HS256 using the project JWT secret on legacy
# projects; newer projects use asymmetric JWT Signing Keys (ES256).
_HS256_ALGORITHMS = ["HS256"]
_ASYMMETRIC_ALGORITHMS = ("ES256", "RS256")
_LEEWAY_SECONDS = 30

# One JWKS client per process; PyJWKClient caches the key set (default 5 min)
# and refetches when a token's kid is not in the cache (key rotation).
_jwks_client: PyJWKClient | None = None
_jwks_uri: str | None = None


def _jwks_uri_for_base(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def _get_jwks_client() -> PyJWKClient:
    """Lazily build (and rebuild on URL change) the process-wide JWKS client."""
    global _jwks_client, _jwks_uri
    uri = _jwks_uri_for_base(settings.supabase_url)
    if _jwks_client is None or _jwks_uri != uri:
        _jwks_client = PyJWKClient(uri, cache_keys=True)
        _jwks_uri = uri
    return _jwks_client

_UNSUPPORTED_NOTE = (
    "Sign-in, sign-up, password reset, and session refresh are handled by "
    "Supabase in the browser (supabase-js). AURA verifies the resulting access "
    "token and mirrors the account locally."
)


class SupabaseAuthBackend(AuthBackend):
    """Verifies Supabase-issued JWTs and mirrors the user locally."""

    name = "supabase"

    @property
    def capabilities(self) -> AuthCapabilities:
        return AuthCapabilities(
            provider=self.name,
            can_register=False,
            can_login_with_password=False,
            can_refresh=False,
            can_reset_password=False,
            can_change_password=False,
            # Revocation is a Supabase concern; AURA holds no session rows here.
            can_revoke_sessions=False,
            notes=_UNSUPPORTED_NOTE,
        )

    # ------------------------------------------------------------ token verify
    def verify_token(self, token: str) -> AuthClaims:
        try:
            algorithm = jwt.get_unverified_header(token).get("alg")
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError(
                "That sign-in token is not valid.", code="token_invalid"
            ) from exc

        if algorithm in _ASYMMETRIC_ALGORITHMS:
            payload = self._verify_asymmetric(token, algorithm)
        elif algorithm == "HS256":
            payload = self._verify_hs256(token)
        else:
            log.warning("supabase_token_rejected", reason="unsupported_algorithm")
            raise AuthenticationError(
                "That sign-in token is not valid.", code="token_invalid"
            )

        subject = str(payload.get("sub") or "").strip()
        if not subject:
            raise AuthenticationError(
                "That sign-in token is not valid.", code="token_invalid"
            )

        issued_raw = payload.get("iat")
        issued_at = (
            datetime.fromtimestamp(int(issued_raw), tz=UTC)
            if issued_raw is not None
            else datetime.now(UTC)
        )

        return AuthClaims(
            subject=subject,
            provider=self.name,
            issued_at=issued_at,
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            email=(payload.get("email") or None),
            # Supabase manages its own sessions; there is no local session row.
            session_id=None,
            raw=payload,
        )

    def _verify_hs256(self, token: str) -> dict[str, Any]:
        """Legacy projects: verify against the shared JWT secret (offline)."""
        secret = settings.supabase_jwt_secret
        if not secret:
            # Configuration already refuses to start in this state; this guard
            # exists so a mis-wired test cannot silently accept unverified tokens.
            raise AuthenticationError(
                "Supabase sign-in is not configured on this installation.",
                code="auth_not_configured",
            )

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                secret,
                algorithms=_HS256_ALGORITHMS,
                audience=settings.supabase_jwt_audience,
                leeway=_LEEWAY_SECONDS,
                options={
                    "require": ["exp", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(
                "Your session has expired. Please sign in again.",
                code="token_expired",
            ) from exc
        except jwt.InvalidTokenError as exc:
            log.warning("supabase_token_rejected", reason=type(exc).__name__)
            raise AuthenticationError(
                "That sign-in token is not valid.", code="token_invalid"
            ) from exc
        return payload

    def _verify_asymmetric(self, token: str, algorithm: str) -> dict[str, Any]:
        """Newer projects: verify an ES256/RS256 token against the JWKS.

        The signing key is selected by the token's ``kid`` and the issuer is
        pinned to this project's ``/auth/v1`` so a token minted by any other
        Supabase project is rejected.
        """
        base_url = (settings.supabase_url or "").rstrip("/")
        if not base_url:
            raise AuthenticationError(
                "Supabase sign-in is not configured on this installation.",
                code="auth_not_configured",
            )

        try:
            signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        except PyJWKClientError as exc:
            # A kid this project never published means the token is not ours;
            # anything else (network, malformed JWKS) is transient.
            if "Unable to find a signing key" in str(exc):
                log.warning("supabase_token_rejected", reason="unknown_kid")
                raise AuthenticationError(
                    "That sign-in token is not valid.", code="token_invalid"
                ) from exc
            log.warning("supabase_jwks_unavailable", reason=type(exc).__name__)
            raise AuthenticationError(
                "Could not verify the sign-in token right now. Please try again.",
                code="token_verification_unavailable",
            ) from exc

        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=[algorithm],
                audience=settings.supabase_jwt_audience,
                issuer=f"{base_url}/auth/v1",
                leeway=_LEEWAY_SECONDS,
                options={
                    "require": ["exp", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError(
                "Your session has expired. Please sign in again.",
                code="token_expired",
            ) from exc
        except jwt.InvalidTokenError as exc:
            log.warning("supabase_token_rejected", reason=type(exc).__name__)
            raise AuthenticationError(
                "That sign-in token is not valid.", code="token_invalid"
            ) from exc
        return payload

    # -------------------------------------------------------------- user mirror
    def resolve_user(self, session: Session, claims: AuthClaims) -> User:
        """Find or create the local mirror of a Supabase account.

        Created on first authenticated request rather than via a webhook, so the
        two systems cannot drift into a state where a signed-in user has no local
        row. ``password_hash`` stays null -- the CHECK constraint on ``users``
        accepts the row because ``supabase_user_id`` is set.
        """
        user = session.scalar(
            select(User).where(User.supabase_user_id == claims.subject)
        )
        if user is not None:
            if not user.is_active:
                raise AuthenticationError(
                    "This account has been deactivated.", code="account_inactive"
                )
            # Keep the mirrored email current if it was changed in Supabase.
            if claims.email and user.email != claims.email.lower():
                user.email = claims.email.lower()
            return user

        if not claims.email:
            # Without an email there is nothing to key the local row on, and
            # `users.email` is NOT NULL.
            raise AuthenticationError(
                "That sign-in token is missing an email address.",
                code="token_missing_email",
            )

        address = claims.email.strip().lower()

        # An account may already exist locally from a previous local-provider run
        # with the same address. Link it rather than colliding on the unique
        # index, which would otherwise lock the user out permanently.
        existing = session.scalar(select(User).where(User.email == address))
        if existing is not None:
            existing.supabase_user_id = claims.subject
            log.info(
                "supabase_user_linked",
                user_id=str(existing.id),
                reason="existing_local_email",
            )
            return existing

        metadata = claims.raw.get("user_metadata") or {}
        full_name = None
        if isinstance(metadata, dict):
            raw_name = metadata.get("full_name") or metadata.get("name")
            full_name = str(raw_name).strip() or None if raw_name else None

        user = User(
            email=address,
            full_name=full_name,
            supabase_user_id=claims.subject,
            password_hash=None,
            is_active=True,
            # Supabase records this; trust its claim rather than inventing one.
            is_email_verified=bool(
                claims.raw.get("email_confirmed_at")
                or claims.raw.get("email_verified")
                or (isinstance(metadata, dict) and metadata.get("email_verified"))
            ),
        )
        session.add(user)
        session.flush()
        log.info("supabase_user_mirrored", user_id=str(user.id))
        return user

    # ------------------------------------------------------------------ logout
    def logout(self, session: Session, *, claims: AuthClaims) -> bool:
        """No-op by design, and reported as such.

        Returning ``False`` tells the route that nothing was revoked server-side,
        so the response can say honestly that the client must call
        ``supabase.auth.signOut()``. It does not pretend to have ended a session
        it has no authority over.
        """
        return False


def parse_supabase_user_id(value: str) -> str:
    """Validate that a Supabase ``sub`` looks like the UUID it should be."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


__all__ = ["SupabaseAuthBackend"]
