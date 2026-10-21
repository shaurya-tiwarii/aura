"""Request and response schemas for authentication endpoints.

Two rules shape everything here:

* **Passwords and tokens are input-only.** No response model carries a password,
  a password hash, or a refresh token hash. ``UserOut`` is the single shape any
  endpoint uses to describe a user, so a new field cannot leak by accident from
  one route that forgot to narrow its output.
* **Errors are shaped by** :mod:`app.core.errors`, not here, so every failure the
  frontend sees uses one envelope.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.auth.local import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH


class RegisterRequest(BaseModel):
    email: EmailStr
    # Bounds are enforced here for a fast, field-level error, and again in
    # `validate_password_strength` which owns the real policy (and runs even when
    # a caller bypasses this schema).
    password: str = Field(
        min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )
    full_name: str | None = Field(default=None, max_length=200)

    @field_validator("full_name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class LoginRequest(BaseModel):
    email: EmailStr
    # No min_length: a short password is a failed login, not a validation error.
    # Enforcing length here would let a caller distinguish "too short" from
    # "wrong", and would reject a legacy password that predates the policy.
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(
        min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=16, max_length=512)
    new_password: str = Field(
        min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH
    )


class UserOut(BaseModel):
    """The only user shape returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    is_active: bool
    is_email_verified: bool
    theme: str
    timezone: str
    last_login_at: datetime | None
    created_at: datetime


class TokenOut(BaseModel):
    """OAuth2-ish token response.

    ``refresh_token`` is null under Supabase auth, where refresh is the browser
    client's job -- the field is present so the shape is stable, not because a
    value is being withheld.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    refresh_token: str | None = None
    refresh_expires_in: int | None = None


class AuthSessionOut(BaseModel):
    """A successful login or refresh: who you are plus how to prove it."""

    user: UserOut
    tokens: TokenOut


class SessionOut(BaseModel):
    """One active device session, for the security page."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    # True for the session making the request, so the UI can label "this device".
    is_current: bool = False


class CapabilitiesOut(BaseModel):
    """Which auth operations this installation can actually perform.

    The frontend renders controls from this rather than assuming, so a Supabase
    deployment does not show a registration form the backend would reject.
    """

    provider: str
    can_register: bool
    can_login_with_password: bool
    can_refresh: bool
    can_reset_password: bool
    can_change_password: bool
    can_revoke_sessions: bool
    notes: str = ""


class MessageOut(BaseModel):
    """Generic acknowledgement for actions with no resource to return."""

    status: str
    message: str
    detail: dict[str, Any] | None = None


class ForgotPasswordOut(BaseModel):
    """Response to a reset request.

    Identical whether or not the address is registered, so the endpoint cannot be
    used to enumerate accounts.

    ``delivery`` states honestly what happened to the link. No SMTP transport is
    configured in this build, so it is ``"not_configured"``, and outside
    production the link itself is returned in ``reset_url`` so the flow is
    genuinely completable rather than a dead end. In production the link is never
    returned.
    """

    status: str
    message: str
    delivery: str
    reset_url: str | None = None
