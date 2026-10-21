"""Selects the active authentication backend from configuration."""

from __future__ import annotations

from functools import cache

from app.auth.base import (
    AuthBackend,
    AuthCapabilities,
    AuthClaims,
    RequestContext,
    TokenPair,
)
from app.auth.local import LocalAuthBackend
from app.auth.supabase import SupabaseAuthBackend
from app.core.config import settings

_BACKENDS: dict[str, type[AuthBackend]] = {
    "local": LocalAuthBackend,
    "supabase": SupabaseAuthBackend,
}


@cache
def get_auth_backend(provider: str | None = None) -> AuthBackend:
    """Return the configured backend.

    Cached because backends are stateless -- all per-request state arrives as
    arguments. ``provider`` is only passed explicitly by tests that need to
    exercise the other backend without mutating global settings.
    """
    name = provider or settings.auth_provider
    try:
        backend_cls = _BACKENDS[name]
    except KeyError:  # pragma: no cover - Literal type makes this unreachable
        raise ValueError(
            f"Unknown AUTH_PROVIDER {name!r}; expected one of {sorted(_BACKENDS)}"
        ) from None
    return backend_cls()


__all__ = [
    "AuthBackend",
    "AuthCapabilities",
    "AuthClaims",
    "RequestContext",
    "TokenPair",
    "get_auth_backend",
]
