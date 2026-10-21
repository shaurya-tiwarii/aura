"""Application configuration.

All configuration is sourced from environment variables (never hard-coded) and
validated at import time so that a misconfigured deployment fails immediately
and loudly rather than at the first request.

Design notes
------------
* Every setting has a working local default *except* those that would be a
  security hazard if defaulted (``SECRET_KEY`` in production, the Supabase JWT
  secret when Supabase auth is selected). Those are enforced in
  :meth:`Settings._validate_consistency`.
* Absent third-party credentials are never fatal. They flip a capability flag
  (``ai_enabled``, ``connector_configured``) which the API surfaces to the
  frontend so the UI can state plainly what is and is not available.
"""

from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

INSECURE_SECRET_KEY = "dev-only-insecure-change-me"

AuthProvider = Literal["local", "supabase"]
AppEnv = Literal["development", "production", "test"]
LogFormat = Literal["console", "json"]


class Settings(BaseSettings):
    """Typed, validated view of the process environment."""

    model_config = SettingsConfigDict(
        # `.env` next to the backend package, then the repo root, so a single
        # root-level .env works for both docker-compose and bare local runs.
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ core
    app_env: AppEnv = "development"
    app_name: str = "AURA"
    app_version: str = "1.0.0"
    log_level: str = "INFO"
    log_format: LogFormat = "console"
    secret_key: str = INSECURE_SECRET_KEY
    # ``NoDecode`` is load-bearing. pydantic-settings treats any ``list``-typed
    # field as "complex" and runs ``json.loads`` on the raw env/dotenv value
    # *before* field validators execute, so the documented comma-separated form
    # (``BACKEND_CORS_ORIGINS=http://a,http://b``) would raise a JSONDecodeError
    # at import time. Opting out of that decoding hands the raw string to
    # :meth:`_split_origins`, which accepts both the comma form and a JSON array.
    backend_cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    # -------------------------------------------------------------- database
    database_url: str = ""

    # ------------------------------------------------------------------ auth
    auth_provider: AuthProvider = "local"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    # Supabase's newer API-key naming. Accepted as a deprecated alias for
    # SUPABASE_ANON_KEY: if the anon key is empty, the publishable key is used
    # in its place. SUPABASE_ANON_KEY remains the canonical name.
    supabase_publishable_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"
    access_token_ttl_minutes: int = Field(default=60, ge=5, le=1440)
    refresh_token_ttl_days: int = Field(default=14, ge=1, le=90)

    # -------------------------------------------------------------------- ai
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: int = Field(default=60, ge=5, le=300)
    gemini_max_retries: int = Field(default=3, ge=0, le=6)
    gemini_max_output_tokens: int = Field(default=8192, ge=256, le=65536)

    # --------------------------------------------------------------- crawler
    crawler_max_pages: int = Field(default=20, ge=1, le=20)
    crawler_max_depth: int = Field(default=3, ge=0, le=6)
    crawler_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    crawler_delay_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    crawler_max_concurrency: int = Field(default=4, ge=1, le=16)
    crawler_max_response_bytes: int = Field(default=5 * 1024 * 1024, ge=8192)
    # Outbound-link verification after the crawl: at most this many distinct
    # external URLs are probed per scan (HEAD, no body download, same SSRF
    # guards as the crawl). 0 disables the check; PageExtraction rows beyond
    # the cap keep broken_link_count=0, honestly "not checked".
    crawler_max_link_checks: int = Field(default=50, ge=0, le=200)
    crawler_respect_robots: bool = True
    crawler_user_agent: str = "AURABot/1.0 (+https://github.com/aura-platform/aura)"
    # SSRF guard. Only ever true inside the test suite.
    crawler_allow_private_hosts: bool = False

    # ------------------------------------------------------------ encryption
    token_encryption_key: str = ""

    # ------------------------------------------------------------ connectors
    public_base_url: str = "http://localhost:8000"
    wordpress_client_id: str = ""
    wordpress_client_secret: str = ""
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "web:aura:1.0"

    # ------------------------------------------------------------- scheduler
    scheduler_enabled: bool = True
    scheduler_timezone: str = "UTC"
    scheduler_max_retries: int = Field(default=3, ge=0, le=10)
    scheduler_retry_backoff_seconds: int = Field(default=300, ge=10)
    scheduler_misfire_grace_seconds: int = Field(default=3600, ge=60)

    # ---------------------------------------------------------- rate limiting
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = Field(default=120, ge=1)
    rate_limit_auth_per_minute: int = Field(default=20, ge=1)
    rate_limit_analysis_per_hour: int = Field(default=20, ge=1)
    rate_limit_ai_per_hour: int = Field(default=60, ge=1)

    # ------------------------------------------------------------- validators
    @field_validator("backend_cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept a comma-separated string, a JSON array, or a real list.

        Because the field is annotated ``NoDecode``, this validator receives the
        raw dotenv/env string and owns *all* parsing for it -- including the JSON
        form, which pydantic-settings would otherwise have handled.
        """
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "BACKEND_CORS_ORIGINS looks like a JSON array but is not "
                        f"valid JSON: {exc.msg}"
                    ) from exc
                if not isinstance(parsed, list):
                    raise ValueError("BACKEND_CORS_ORIGINS JSON must be an array")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            level = value.strip().upper()
            allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
            if level not in allowed:
                raise ValueError(f"log_level must be one of {sorted(allowed)}")
            return level
        return value

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_db_url(cls, value: Any) -> Any:
        """Upgrade bare ``postgresql://``/``postgres://`` URLs to the psycopg v3 driver.

        Supabase and Render hand out ``postgresql://`` or ``postgres://``
        connection strings, which SQLAlchemy would route to psycopg2
        (not installed). Rewriting here means the documented copy-paste
        string works unmodified.
        """
        if isinstance(value, str) and value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+psycopg://", 1)
        if isinstance(value, str) and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg://", 1)
        return value

    @field_validator("public_base_url", "supabase_url", mode="before")
    @classmethod
    def _strip_trailing_slash(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.rstrip("/")
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        """Cross-field checks that must hold before the app serves traffic."""
        # Deprecated alias: the newer Supabase publishable-key naming falls
        # back to the canonical anon-key slot.
        if not self.supabase_anon_key and self.supabase_publishable_key:
            self.supabase_anon_key = self.supabase_publishable_key
        problems: list[str] = []

        if self.is_production:
            if self.secret_key == INSECURE_SECRET_KEY or len(self.secret_key) < 32:
                problems.append(
                    "SECRET_KEY must be set to a random value of at least 32 "
                    "characters when APP_ENV=production"
                )
            if not self.database_url:
                problems.append("DATABASE_URL is required when APP_ENV=production")
            if self.crawler_allow_private_hosts:
                problems.append(
                    "CRAWLER_ALLOW_PRIVATE_HOSTS must be false in production "
                    "(it disables the SSRF guard)"
                )

        if self.auth_provider == "supabase":
            missing = [
                name
                for name, val in (
                    ("SUPABASE_URL", self.supabase_url),
                    ("SUPABASE_ANON_KEY", self.supabase_anon_key),
                )
                if not val
            ]
            if missing:
                problems.append(
                    "AUTH_PROVIDER=supabase requires: "
                    + ", ".join(missing)
                    + " (SUPABASE_PUBLISHABLE_KEY is accepted as an alias for "
                    "SUPABASE_ANON_KEY; SUPABASE_JWT_SECRET is only needed for "
                    "legacy HS256 projects -- newer projects verify via JWKS)"
                )

        if problems:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(problems))
        return self

    # ------------------------------------------------------- derived helpers
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "test"

    @property
    def resolved_database_url(self) -> str:
        """The database URL actually used, with a zero-setup fallback.

        Postgres is the supported target. When ``DATABASE_URL`` is empty we fall
        back to a local SQLite file so that a fresh clone runs with no database
        server installed. This is a convenience for development and offline
        demos; production is required to set an explicit Postgres URL (enforced
        in :meth:`_validate_consistency`).
        """
        if self.database_url:
            return self.database_url
        return f"sqlite+pysqlite:///{(BACKEND_DIR / 'aura.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")

    @property
    def ai_enabled(self) -> bool:
        """Whether Gemini-backed features can run at all."""
        return bool(self.gemini_api_key)

    @property
    def token_encryption_enabled(self) -> bool:
        return bool(self.token_encryption_key)

    def connector_credentials(self, platform: str) -> tuple[str, str]:
        """Return ``(client_id, client_secret)`` for a connector platform."""
        mapping = {
            "wordpress": (self.wordpress_client_id, self.wordpress_client_secret),
            "linkedin": (self.linkedin_client_id, self.linkedin_client_secret),
            "reddit": (self.reddit_client_id, self.reddit_client_secret),
        }
        return mapping.get(platform.lower(), ("", ""))

    def connector_configured(self, platform: str) -> bool:
        """True when real API credentials exist for *platform*.

        Drives the real-vs-demo decision in the publishing layer. A connector
        without credentials still exposes its full workflow, but every result it
        produces is explicitly marked simulated.
        """
        client_id, client_secret = self.connector_credentials(platform)
        return bool(client_id and client_secret)

    def capability_report(self) -> dict[str, Any]:
        """Machine-readable capability summary consumed by ``GET /meta/capabilities``.

        The frontend uses this to tell the user exactly which features are live,
        which are running in demo mode, and which credential would enable them —
        rather than showing buttons that silently do nothing.
        """
        return {
            "environment": self.app_env,
            "auth_provider": self.auth_provider,
            "database": "postgresql" if not self.is_sqlite else "sqlite",
            "ai": {
                "enabled": self.ai_enabled,
                "model": self.gemini_model if self.ai_enabled else None,
                "required_credential": None if self.ai_enabled else "GEMINI_API_KEY",
            },
            "token_encryption": {
                "enabled": self.token_encryption_enabled,
                "required_credential": (
                    None if self.token_encryption_enabled else "TOKEN_ENCRYPTION_KEY"
                ),
            },
            "connectors": {
                platform: {
                    "mode": "live" if self.connector_configured(platform) else "demo",
                    "required_credentials": (
                        []
                        if self.connector_configured(platform)
                        else [
                            f"{platform.upper()}_CLIENT_ID",
                            f"{platform.upper()}_CLIENT_SECRET",
                        ]
                    ),
                }
                for platform in ("wordpress", "linkedin", "reddit")
            },
            "scheduler": {"enabled": self.scheduler_enabled},
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so the ``.env`` file is read once. Tests clear the cache via
    ``get_settings.cache_clear()`` after patching the environment.
    """
    try:
        return Settings()
    except ValueError as exc:  # pragma: no cover - startup failure path
        # Printed rather than logged: logging is configured *from* settings, so
        # it is not available yet at this point.
        print(  # noqa: T201 - stderr is the only channel available this early
            f"\n[AURA] Configuration error:\n{exc}\n", file=sys.stderr
        )
        raise


settings = get_settings()
