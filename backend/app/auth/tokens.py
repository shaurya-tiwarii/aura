"""JWT minting and verification for the local auth provider.

Design notes that matter for security:

* **HS256 with an explicit allow-list on decode.** Passing ``algorithms=["HS256"]``
  is what prevents the classic ``alg: none`` / algorithm-confusion attack, where
  an attacker re-signs a token with a different algorithm and the library
  obligingly accepts it.
* **``iss`` and ``aud`` are verified, not just present.** Without that, a token
  minted by some other service that happens to share the secret would be
  accepted here.
* **Token type is part of the payload and is checked.** A refresh token must not
  be usable as an access token; they have very different lifetimes.
* **``leeway``** absorbs small clock differences between processes rather than
  rejecting a token that is valid by a fraction of a second.

The refresh token returned to clients is *not* a JWT: it is high-entropy random
bytes whose SHA-256 hash is stored in ``user_sessions``. A JWT would be
self-validating and therefore impossible to revoke before expiry, which is the
opposite of what a refresh token needs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt

from app.core.config import settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger

log = get_logger(__name__)

ISSUER: Final = "aura"
AUDIENCE: Final = "aura-api"
ALGORITHM: Final = "HS256"
ACCESS_TOKEN_TYPE: Final = "access"

# Tolerated clock skew between the signing and verifying process.
_LEEWAY: Final = timedelta(seconds=30)


def _now() -> datetime:
    return datetime.now(UTC)


def mint_access_token(
    *,
    user_id: uuid.UUID,
    email: str,
    session_id: uuid.UUID,
    ttl_minutes: int | None = None,
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``.

    ``sid`` binds the token to a ``user_sessions`` row so that revoking the
    session immediately invalidates this token, instead of leaving it usable
    until it expires.
    """
    ttl = timedelta(minutes=ttl_minutes or settings.access_token_ttl_minutes)
    issued = _now()
    expires = issued + ttl
    payload = {
        "sub": str(user_id),
        "email": email,
        "sid": str(session_id),
        "typ": ACCESS_TOKEN_TYPE,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, int(ttl.total_seconds())


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify and decode an access token, or raise ``AuthenticationError``.

    Error messages stay deliberately generic: telling a caller *why* a token was
    rejected helps an attacker more than it helps a user, who can only ever fix
    this by signing in again. The specific reason is logged server-side.
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            # Explicit allow-list: never let the token choose its own algorithm.
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            leeway=_LEEWAY,
            options={
                "require": ["exp", "iat", "sub", "iss", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
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
        log.warning("access_token_rejected", reason=type(exc).__name__)
        raise AuthenticationError(
            "That sign-in token is not valid.", code="token_invalid"
        ) from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        # A refresh token presented as a bearer credential lands here.
        log.warning("access_token_rejected", reason="wrong_token_type")
        raise AuthenticationError(
            "That sign-in token is not valid.", code="token_invalid"
        )
    return payload


def claims_datetime(payload: dict[str, Any], key: str) -> datetime:
    """Read a numeric-date claim as an aware UTC datetime."""
    raw = payload.get(key)
    if raw is None:
        raise AuthenticationError(
            "That sign-in token is not valid.", code="token_invalid"
        )
    return datetime.fromtimestamp(int(raw), tz=UTC)
