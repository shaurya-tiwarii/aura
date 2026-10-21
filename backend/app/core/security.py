"""Cryptographic primitives: password hashing and token encryption at rest.

Two independent concerns live here.

**Password hashing** (local auth provider only) uses Argon2id, the PHC winner and
current OWASP first choice. When ``AUTH_PROVIDER=supabase``, Supabase owns
password storage and none of this runs.

**Token encryption** protects third-party OAuth access/refresh tokens stored in
``platform_connections``. A database dump alone must not be enough to post to a
user's LinkedIn or WordPress account, so tokens are encrypted with Fernet
(AES-128-CBC + HMAC-SHA256) under a key held only in the environment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

# OWASP-recommended baseline: ~64 MiB memory, 3 iterations, 4 lanes. Chosen to
# make offline cracking expensive while keeping login latency acceptable on the
# free hosting tiers this project targets.
_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


# --------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    """Hash a plaintext password. The plaintext is never stored or logged."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish password check. Returns False rather than raising."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when a stored hash predates the current cost parameters.

    Lets us transparently upgrade hashes on successful login if the parameters
    are ever raised.
    """
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


# ------------------------------------------------------------ random tokens
def generate_token(nbytes: int = 32) -> str:
    """URL-safe random token for password resets, OAuth state, and API nonces."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Hash a bearer-style token for storage.

    Password-reset tokens are stored hashed for the same reason passwords are:
    a leaked database should not hand over working reset links. SHA-256 (not
    Argon2) is correct here because the input is already high-entropy random, so
    there is nothing to brute force and we want lookups to stay cheap.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, to avoid leaking prefixes via timing."""
    return hmac.compare_digest(left, right)


# ------------------------------------------------------- token encryption
class TokenVault:
    """Encrypts/decrypts third-party credentials before they touch the database.

    When ``TOKEN_ENCRYPTION_KEY`` is absent the vault is *disabled* and refuses
    to store secrets rather than silently writing them in plaintext. The
    publishing layer checks :attr:`enabled` and reports the missing credential to
    the user instead of failing obscurely at insert time.
    """

    def __init__(self, key: str | None = None) -> None:
        raw_key = key if key is not None else settings.token_encryption_key
        self._fernet: Fernet | None = None
        if raw_key:
            self._fernet = Fernet(self._coerce_key(raw_key))

    @staticmethod
    def _coerce_key(raw_key: str) -> bytes:
        """Accept a proper Fernet key, or derive one from an arbitrary string.

        Deriving via SHA-256 means a user who pastes a random passphrase instead
        of a generated Fernet key still gets working encryption rather than a
        crash on startup.
        """
        candidate = raw_key.strip().encode("utf-8")
        try:
            Fernet(candidate)
            return candidate
        except (ValueError, TypeError):
            digest = hashlib.sha256(candidate).digest()
            return base64.urlsafe_b64encode(digest)

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise RuntimeError(
                "TOKEN_ENCRYPTION_KEY is not configured; refusing to store a "
                "third-party credential in plaintext."
            )
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is not configured.")
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            # Almost always means the key was rotated or the row was written
            # under a different key.
            log.error("token_decrypt_failed", reason="invalid_token_or_rotated_key")
            raise RuntimeError(
                "Stored credential could not be decrypted. The encryption key "
                "may have changed; reconnect the account."
            ) from exc

    def encrypt_optional(self, plaintext: str | None) -> str | None:
        return self.encrypt(plaintext) if plaintext else None

    def decrypt_optional(self, ciphertext: str | None) -> str | None:
        return self.decrypt(ciphertext) if ciphertext else None


def get_token_vault() -> TokenVault:
    """Build a vault from current settings (fresh instance per call, cheap)."""
    return TokenVault()


def mask_secret(value: str, *, visible: int = 4) -> str:
    """Render a credential for display: ``sk-live-...a1b2``.

    Used by the settings UI to confirm *which* key is configured without ever
    sending the key itself to the browser.
    """
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{'*' * 8}{value[-visible:]}"
