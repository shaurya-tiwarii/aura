"""Authentication: one interface, two real providers.

``AUTH_PROVIDER`` selects the backend; see :mod:`app.auth.base` for the contract
and :mod:`app.auth.registry` for selection.
"""

from __future__ import annotations

from app.auth.base import (
    AuthBackend,
    AuthCapabilities,
    AuthClaims,
    RequestContext,
    TokenPair,
)
from app.auth.local import LocalAuthBackend
from app.auth.registry import get_auth_backend
from app.auth.supabase import SupabaseAuthBackend

__all__ = [
    "AuthBackend",
    "AuthCapabilities",
    "AuthClaims",
    "LocalAuthBackend",
    "RequestContext",
    "SupabaseAuthBackend",
    "TokenPair",
    "get_auth_backend",
]
