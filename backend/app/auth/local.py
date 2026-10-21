"""Local authentication provider: AURA owns the credentials.

Active when ``AUTH_PROVIDER=local``. Needs no external account, which is what
lets the project run offline and makes the demo reproducible.

The security model:

* Passwords hashed with Argon2id (:mod:`app.core.security`), rehashed
  transparently if the cost parameters are ever raised.
* Short-lived JWT access tokens carrying ``sid``, re-checked against
  ``user_sessions`` on every request, so revocation is immediate.
* Long-lived opaque refresh tokens, stored only as SHA-256 hashes, **rotated on
  every use** with replay detection: a rotated token presented a second time
  means it was captured, so the whole family is revoked.
* Login timing is equalised so a wrong password on a real account is
  indistinguishable from any password on an account that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.base import (
    AuthBackend,
    AuthCapabilities,
    AuthClaims,
    RequestContext,
    TokenPair,
)
from app.auth.tokens import ACCESS_TOKEN_TYPE, decode_access_token, mint_access_token
from app.core.config import settings
from app.core.database import session_scope
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.core.security import (
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.models.user import PasswordResetToken, User, UserSession

log = get_logger(__name__)

# Long enough that guessing is hopeless; the value is never stored in the clear.
_REFRESH_TOKEN_BYTES = 48
_RESET_TOKEN_BYTES = 32
_RESET_TTL = timedelta(hours=1)

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

# A precomputed Argon2id hash of a value nobody knows. Verified against when the
# email does not exist, so a missing account costs the same time as a wrong
# password and cannot be detected by timing. Computed lazily once per process.
_DUMMY_HASH: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(generate_token(16))
    return _DUMMY_HASH


def validate_password_strength(password: str) -> None:
    """Reject passwords that are trivially weak.

    Deliberately length-first rather than a composition rule ("one symbol, one
    digit"): length is what actually resists offline cracking, and composition
    rules push users toward predictable substitutions. NIST SP 800-63B says the
    same.
    """
    problems: dict[str, str] = {}
    if len(password) < MIN_PASSWORD_LENGTH:
        problems["password"] = f"must be at least {MIN_PASSWORD_LENGTH} characters long"
    elif len(password) > MAX_PASSWORD_LENGTH:
        # Bounded so a huge input cannot be used to burn CPU in Argon2.
        problems["password"] = f"must be at most {MAX_PASSWORD_LENGTH} characters long"
    elif password.lower() in _COMMON_PASSWORDS:
        problems["password"] = "is too common; please choose something less guessable"
    elif len(set(password)) < 4:
        problems["password"] = "must not repeat the same few characters"

    if problems:
        raise ValidationFailedError(
            "That password is not strong enough.", fields=problems
        )


# Small embedded list rather than a dependency: catches the passwords that
# actually show up in credential-stuffing lists at this length.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password12",
        "password123",
        "password1234",
        "passw0rd123",
        "qwerty12345",
        "qwertyuiop",
        "1234567890",
        "12345678901",
        "123456789012",
        "letmein123",
        "welcome123",
        "iloveyou123",
        "admin12345",
        "administrator",
        "changeme123",
        "aura123456",
    }
)


def normalise_email(email: str) -> str:
    """Lower-case and trim.

    The local part of an email is technically case-sensitive, but treating it as
    such invites duplicate accounts for the same human. Every mainstream provider
    folds case, so AURA does too -- and consistently, so lookups always match
    what was stored.
    """
    return email.strip().lower()


class LocalAuthBackend(AuthBackend):
    """Self-contained email + password authentication."""

    name = "local"

    @property
    def capabilities(self) -> AuthCapabilities:
        return AuthCapabilities(
            provider=self.name,
            can_register=True,
            can_login_with_password=True,
            can_refresh=True,
            can_reset_password=True,
            can_change_password=True,
            can_revoke_sessions=True,
            notes=(
                "AURA manages sign-in locally with Argon2id password hashing. "
                "Password-reset emails require an SMTP transport, which is not "
                "configured on this installation."
            ),
        )

    # ------------------------------------------------------------ token verify
    def verify_token(self, token: str) -> AuthClaims:
        payload = decode_access_token(token)
        try:
            subject = str(payload["sub"])
            uuid.UUID(subject)
        except (KeyError, ValueError) as exc:
            raise AuthenticationError(
                "That sign-in token is not valid.", code="token_invalid"
            ) from exc

        session_id: uuid.UUID | None = None
        raw_sid = payload.get("sid")
        if raw_sid:
            try:
                session_id = uuid.UUID(str(raw_sid))
            except ValueError as exc:
                raise AuthenticationError(
                    "That sign-in token is not valid.", code="token_invalid"
                ) from exc

        return AuthClaims(
            subject=subject,
            provider=self.name,
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
            email=payload.get("email"),
            session_id=session_id,
            raw=payload,
        )

    def resolve_user(self, session: Session, claims: AuthClaims) -> User:
        """Load the user and confirm the backing session is still live.

        The session check is the difference between a token that *looks* valid and
        one that *is* valid: without it, signing out would leave the access token
        working until it expired.
        """
        user = session.get(User, uuid.UUID(claims.subject))
        if user is None:
            raise AuthenticationError(
                "Your account could not be found.", code="user_not_found"
            )
        if not user.is_active:
            raise AuthenticationError(
                "This account has been deactivated.", code="account_inactive"
            )

        if claims.session_id is not None:
            record = session.get(UserSession, claims.session_id)
            if record is None or record.user_id != user.id:
                raise AuthenticationError(
                    "Your session is no longer valid. Please sign in again.",
                    code="session_unknown",
                )
            if not record.is_active(now=_now()):
                raise AuthenticationError(
                    "Your session has ended. Please sign in again.",
                    code="session_revoked",
                )
        return user

    # ---------------------------------------------------------------- register
    def register(
        self,
        session: Session,
        *,
        email: str,
        password: str,
        full_name: str | None = None,
        context: RequestContext | None = None,
    ) -> User:
        address = normalise_email(email)
        validate_password_strength(password)

        existing = session.scalar(select(User).where(User.email == address))
        if existing is not None:
            # Registration is one place where leaking existence is unavoidable --
            # the alternative is silently not creating an account. The generic
            # message and the reset flow keep it from being a useful oracle.
            raise EmailAlreadyRegisteredError()

        user = User(
            email=address,
            full_name=(full_name or "").strip() or None,
            password_hash=hash_password(password),
            is_active=True,
            # No mail transport is configured, so there is nothing to verify
            # against yet. Left false rather than claiming a verification that
            # never happened.
            is_email_verified=False,
        )
        session.add(user)
        session.flush()
        log.info("user_registered", user_id=str(user.id), provider=self.name)
        return user

    # ------------------------------------------------------------------- login
    def login(
        self,
        session: Session,
        *,
        email: str,
        password: str,
        context: RequestContext | None = None,
    ) -> tuple[User, TokenPair]:
        address = normalise_email(email)
        user = session.scalar(select(User).where(User.email == address))

        # Always run a verification, even with no user, so the response time does
        # not reveal whether the address is registered.
        stored_hash = (
            user.password_hash if user and user.password_hash else _dummy_hash()
        )
        password_ok = verify_password(password, stored_hash)

        if user is None or not user.password_hash or not password_ok:
            log.info("login_failed", email_domain=address.rpartition("@")[2] or None)
            raise InvalidCredentialsError()

        if not user.is_active:
            raise AuthenticationError(
                "This account has been deactivated.", code="account_inactive"
            )

        # Opportunistic upgrade if Argon2 parameters were raised since signup.
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        user.last_login_at = _now()
        pair = self._issue_session(session, user=user, context=context)
        log.info("login_succeeded", user_id=str(user.id))
        return user, pair

    # ----------------------------------------------------------------- refresh
    def refresh(
        self,
        session: Session,
        *,
        refresh_token: str,
        context: RequestContext | None = None,
    ) -> tuple[User, TokenPair]:
        digest = hash_token(refresh_token)
        record = session.scalar(
            select(UserSession).where(UserSession.refresh_token_hash == digest)
        )
        if record is None:
            raise AuthenticationError(
                "That sign-in session is no longer valid. Please sign in again.",
                code="refresh_token_invalid",
            )

        now = _now()

        if record.is_revoked:
            # A token we already rotated is being presented again. The legitimate
            # client cannot do this -- it replaced the token when it rotated. So
            # treat it as a captured token and revoke the entire family, which
            # logs out whoever currently holds the newest one too.
            if record.revoked_reason == "rotated":
                revoked = self._revoke_family_durably(
                    family_id=record.family_id, reason="reuse_detected"
                )
                log.warning(
                    "refresh_token_reuse_detected",
                    user_id=str(record.user_id),
                    family_id=str(record.family_id),
                    sessions_revoked=revoked,
                )
            raise AuthenticationError(
                "That sign-in session is no longer valid. Please sign in again.",
                code="refresh_token_revoked",
            )

        if record.expires_at <= now:
            self._revoke_one_durably(session_id=record.id, reason="expired")
            raise AuthenticationError(
                "Your session has expired. Please sign in again.",
                code="refresh_token_expired",
            )

        user = session.get(User, record.user_id)
        if user is None or not user.is_active:
            self._revoke_one_durably(session_id=record.id, reason="account_inactive")
            raise AuthenticationError(
                "This account is no longer active.", code="account_inactive"
            )

        record.revoke("rotated", now=now)
        record.last_used_at = now
        pair = self._issue_session(
            session, user=user, context=context, family_id=record.family_id
        )
        return user, pair

    # ------------------------------------------------------------------ logout
    def logout(self, session: Session, *, claims: AuthClaims) -> bool:
        if claims.session_id is None:
            return False
        record = session.get(UserSession, claims.session_id)
        if record is None or record.is_revoked:
            return False
        record.revoke("logout", now=_now())
        log.info("logout", user_id=claims.subject)
        return True

    def logout_all(self, session: Session, *, user: User) -> int:
        count = self._revoke_active(session, user_id=user.id, reason="logout_all")
        log.info("logout_all", user_id=str(user.id), sessions_revoked=count)
        return count

    # --------------------------------------------------------------- passwords
    def change_password(
        self,
        session: Session,
        *,
        user: User,
        current_password: str,
        new_password: str,
        keep_session_id: uuid.UUID | None = None,
    ) -> int:
        if not user.password_hash or not verify_password(
            current_password, user.password_hash
        ):
            raise InvalidCredentialsError(
                "Your current password is not correct.",
                code="current_password_incorrect",
            )
        validate_password_strength(new_password)
        if verify_password(new_password, user.password_hash):
            raise ConflictError(
                "That is already your current password.",
                code="password_unchanged",
            )

        user.password_hash = hash_password(new_password)
        # Changing a password must end sessions the user may not control any
        # more. The current one is kept so they are not signed out mid-action.
        revoked = self._revoke_active(
            session,
            user_id=user.id,
            reason="password_change",
            except_session_id=keep_session_id,
        )
        log.info(
            "password_changed", user_id=str(user.id), other_sessions_revoked=revoked
        )
        return revoked

    def request_password_reset(
        self,
        session: Session,
        *,
        email: str,
        context: RequestContext | None = None,
    ) -> str | None:
        """Create a reset grant. Returns the raw token, or None if no such user.

        The caller is responsible for not turning that ``None`` into an
        enumeration oracle -- see the route, which responds identically either
        way.
        """
        address = normalise_email(email)
        user = session.scalar(select(User).where(User.email == address))
        if user is None or not user.is_active:
            log.info("password_reset_requested_unknown_email")
            return None

        # One live grant at a time, so an old link cannot be used after a newer
        # one is requested.
        now = _now()
        for existing in session.scalars(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
            )
        ):
            existing.used_at = now

        raw_token = generate_token(_RESET_TOKEN_BYTES)
        session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=now + _RESET_TTL,
                requested_ip=(context.ip_address if context else None),
            )
        )
        log.info("password_reset_requested", user_id=str(user.id))
        return raw_token

    def reset_password(
        self, session: Session, *, token: str, new_password: str
    ) -> User:
        record = session.scalar(
            select(PasswordResetToken).where(
                PasswordResetToken.token_hash == hash_token(token)
            )
        )
        now = _now()
        if record is None or not record.is_usable(now=now):
            raise NotFoundError(
                "That password reset link is invalid or has expired.",
                code="reset_token_invalid",
                hint="Request a new reset link and try again.",
            )

        validate_password_strength(new_password)
        user = session.get(User, record.user_id)
        if user is None or not user.is_active:
            raise NotFoundError(
                "That password reset link is invalid or has expired.",
                code="reset_token_invalid",
            )

        user.password_hash = hash_password(new_password)
        record.used_at = now
        # A reset is the "I may have been compromised" path, so every existing
        # session goes, with no exception for the caller.
        revoked = self._revoke_active(
            session, user_id=user.id, reason="password_change"
        )
        log.info("password_reset", user_id=str(user.id), sessions_revoked=revoked)
        return user

    # ------------------------------------------------------------------ helper
    def _issue_session(
        self,
        session: Session,
        *,
        user: User,
        context: RequestContext | None,
        family_id: uuid.UUID | None = None,
    ) -> TokenPair:
        """Create a ``user_sessions`` row and mint the matching token pair."""
        now = _now()
        refresh_raw = generate_token(_REFRESH_TOKEN_BYTES)
        refresh_ttl = timedelta(days=settings.refresh_token_ttl_days)

        record = UserSession(
            user_id=user.id,
            refresh_token_hash=hash_token(refresh_raw),
            family_id=family_id or uuid.uuid4(),
            expires_at=now + refresh_ttl,
            last_used_at=now,
            ip_address=(context.ip_address if context else None),
            user_agent=(context.user_agent if context else None),
        )
        session.add(record)
        # Needed before minting: the access token embeds this row's id as `sid`.
        session.flush()

        access_token, expires_in = mint_access_token(
            user_id=user.id, email=user.email, session_id=record.id
        )
        return TokenPair(
            access_token=access_token,
            expires_in=expires_in,
            refresh_token=refresh_raw,
            refresh_expires_in=int(refresh_ttl.total_seconds()),
        )

    def _revoke_active(
        self,
        session: Session,
        *,
        user_id: uuid.UUID,
        reason: str,
        except_session_id: uuid.UUID | None = None,
    ) -> int:
        now = _now()
        count = 0
        for record in session.scalars(
            select(UserSession).where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
        ):
            if except_session_id is not None and record.id == except_session_id:
                continue
            record.revoke(reason, now=now)
            count += 1
        return count

    def _revoke_family(
        self, session: Session, *, family_id: uuid.UUID, reason: str, now: datetime
    ) -> int:
        count = 0
        for record in session.scalars(
            select(UserSession).where(
                UserSession.family_id == family_id,
                UserSession.revoked_at.is_(None),
            )
        ):
            record.revoke(reason, now=now)
            count += 1
        return count

    # The two helpers below exist because of transaction lifetime, not style.
    #
    # ``get_db`` rolls the request's session back on any exception. Every
    # revocation in ``refresh()`` happens on a path that then *raises* -- the
    # token is rejected, so the request fails by design. Writing those
    # revocations to the request session would therefore discard them: the
    # warning would be logged, the row would never change, and reuse detection
    # would be a security control that does nothing. Committing in a separate
    # transaction is what makes it real.

    def _revoke_family_durably(self, *, family_id: uuid.UUID, reason: str) -> int:
        """Revoke every live session in a token family, committed independently."""
        with session_scope() as scoped:
            return self._revoke_family(
                scoped, family_id=family_id, reason=reason, now=_now()
            )

    @staticmethod
    def _revoke_one_durably(*, session_id: uuid.UUID, reason: str) -> None:
        """Revoke a single session, committed independently of the caller."""
        with session_scope() as scoped:
            record = scoped.get(UserSession, session_id)
            if record is not None:
                record.revoke(reason, now=_now())

    # Exposed for the "active sessions" list in settings.
    def active_sessions(self, session: Session, *, user: User) -> list[UserSession]:
        now = _now()
        return [
            record
            for record in session.scalars(
                select(UserSession)
                .where(
                    UserSession.user_id == user.id,
                    UserSession.revoked_at.is_(None),
                )
                .order_by(UserSession.created_at.desc())
            )
            if record.expires_at > now
        ]

    def revoke_session(
        self, session: Session, *, user: User, session_id: uuid.UUID
    ) -> None:
        """Sign one device out.

        Ownership is checked here rather than in the route, and a session that
        belongs to another account raises the same ``NotFoundError`` as one that
        does not exist -- otherwise the difference in response would confirm which
        session ids are real.
        """
        record = session.get(UserSession, session_id)
        if record is None or record.user_id != user.id:
            raise NotFoundError(
                "That session could not be found.",
                code="session_not_found",
            )
        # Idempotent: revoking an already-revoked session is a no-op, so a retried
        # request does not turn into an error the user has to interpret.
        record.revoke("logout", now=_now())
        log.info("session_revoked", user_id=str(user.id))


__all__ = [
    "ACCESS_TOKEN_TYPE",
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "LocalAuthBackend",
    "normalise_email",
    "validate_password_strength",
]
