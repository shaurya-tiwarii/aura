"""The provider-agnostic authentication contract.

AURA supports two identity sources and the rest of the application must not care
which is active:

* ``local``    -- AURA owns credentials (Argon2id) and issues its own JWTs.
* ``supabase`` -- Supabase owns credentials; AURA only *verifies* the JWTs it
  issues.

Those two are genuinely different in what they can do, and pretending otherwise
would be exactly the kind of fake functionality the spec forbids. A backend
therefore advertises its capabilities, and the API layer turns an unsupported
operation into a clear, actionable error ("registration is handled by Supabase")
rather than a silently broken endpoint.

Nothing here touches FastAPI. Backends operate on a SQLAlchemy ``Session`` and
plain dataclasses so the same logic is callable from scripts and tests.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.user import User, UserSession


@dataclass(frozen=True, slots=True)
class AuthClaims:
    """A verified identity, normalised across providers.

    ``subject`` is the identifier the *provider* uses: AURA's own user UUID for
    the local backend, and the Supabase ``sub`` claim under Supabase. It is not
    interchangeable with ``User.id`` -- use :meth:`AuthBackend.resolve_user` to
    go from claims to a local row.
    """

    subject: str
    provider: str
    issued_at: datetime
    expires_at: datetime
    email: str | None = None
    # Local provider only: the user_sessions row backing this token, which is
    # what makes server-side revocation possible. None under Supabase.
    session_id: uuid.UUID | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True, slots=True)
class TokenPair:
    """Credentials handed back to a client after a successful login/refresh."""

    access_token: str
    expires_in: int  # seconds until access_token expires
    refresh_token: str | None = None
    refresh_expires_in: int | None = None
    token_type: str = "bearer"


@dataclass(frozen=True, slots=True)
class AuthCapabilities:
    """What this backend can actually do.

    Surfaced verbatim at ``GET /auth/capabilities`` so the frontend can render
    the real set of controls instead of buttons that cannot work.
    """

    provider: str
    can_register: bool
    can_login_with_password: bool
    can_refresh: bool
    can_reset_password: bool
    can_change_password: bool
    can_revoke_sessions: bool
    # Human-readable explanation shown wherever a capability is unavailable.
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "can_register": self.can_register,
            "can_login_with_password": self.can_login_with_password,
            "can_refresh": self.can_refresh,
            "can_reset_password": self.can_reset_password,
            "can_change_password": self.can_change_password,
            "can_revoke_sessions": self.can_revoke_sessions,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Client metadata recorded against a session, for the device list.

    Purely descriptive. Never used in an authorisation decision, because both
    values are attacker-controlled.
    """

    ip_address: str | None = None
    user_agent: str | None = None


class AuthBackend(ABC):
    """One identity provider.

    Subclasses must implement :meth:`verify_token` and :meth:`resolve_user`;
    everything else raises :class:`~app.core.errors.AuraError` by default so an
    unimplemented capability fails loudly and legibly rather than returning a
    plausible-looking empty result.
    """

    name: str

    @property
    @abstractmethod
    def capabilities(self) -> AuthCapabilities: ...

    # --------------------------------------------------------------- required
    @abstractmethod
    def verify_token(self, token: str) -> AuthClaims:
        """Validate an access token's signature, expiry, and shape.

        Raises :class:`~app.core.errors.AuthenticationError` on any problem.
        Must never return partially-trusted claims.
        """

    @abstractmethod
    def resolve_user(self, session: Session, claims: AuthClaims) -> User:
        """Map verified claims onto the local ``users`` row."""

    # --------------------------------------------------------------- optional
    def register(
        self,
        session: Session,
        *,
        email: str,
        password: str,
        full_name: str | None = None,
        context: RequestContext | None = None,
    ) -> User:
        raise self._unsupported("register")

    def login(
        self,
        session: Session,
        *,
        email: str,
        password: str,
        context: RequestContext | None = None,
    ) -> tuple[User, TokenPair]:
        raise self._unsupported("login")

    def refresh(
        self,
        session: Session,
        *,
        refresh_token: str,
        context: RequestContext | None = None,
    ) -> tuple[User, TokenPair]:
        raise self._unsupported("refresh")

    def logout(self, session: Session, *, claims: AuthClaims) -> bool:
        raise self._unsupported("logout")

    def logout_all(self, session: Session, *, user: User) -> int:
        raise self._unsupported("logout_all")

    def active_sessions(self, session: Session, *, user: User) -> list[UserSession]:
        """Sessions AURA can see and revoke.

        Empty by default, and that is an accurate answer rather than a stub: under
        a provider that owns its own sessions, AURA genuinely has no visibility
        into them. ``capabilities.can_revoke_sessions`` is false in that case, so
        the frontend explains the situation instead of rendering an empty table.
        """
        return []

    def revoke_session(
        self, session: Session, *, user: User, session_id: uuid.UUID
    ) -> None:
        """Revoke one of *user*'s sessions.

        Implementations must raise ``NotFoundError`` both when the id does not
        exist and when it belongs to someone else -- distinguishing the two would
        turn this into an oracle for valid session ids.
        """
        raise self._unsupported("revoke_session")

    def change_password(
        self,
        session: Session,
        *,
        user: User,
        current_password: str,
        new_password: str,
        keep_session_id: uuid.UUID | None = None,
    ) -> int:
        raise self._unsupported("change_password")

    def request_password_reset(
        self,
        session: Session,
        *,
        email: str,
        context: RequestContext | None = None,
    ) -> str | None:
        raise self._unsupported("request_password_reset")

    def reset_password(
        self, session: Session, *, token: str, new_password: str
    ) -> User:
        raise self._unsupported("reset_password")

    # ------------------------------------------------------------------ util
    def _unsupported(self, operation: str) -> Exception:
        from app.core.errors import AuraError

        return AuraError(
            f"{operation.replace('_', ' ').capitalize()} is not available with "
            f"the {self.name} sign-in provider.",
            code="auth_operation_unsupported",
            status_code=400,
            hint=self.capabilities.notes or None,
        )
