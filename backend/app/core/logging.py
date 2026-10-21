"""Structured logging.

Two output modes:

* ``console`` - human-readable, coloured, for local development.
* ``json``    - one JSON object per line, for production log aggregation.

Secret redaction
----------------
Spec sections 20 and 30 require that secrets never reach the logs. Rather than
relying on every call site to remember, :func:`_redact_processor` runs over every
event dictionary and masks any key whose name looks sensitive, at any nesting
depth. That makes leaking a secret an opt-out mistake instead of an opt-in one.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

# `structlog.stdlib` probes a ``logging.LogRecord`` object at import time.
# That constructor asks the stdlib's asyncio task hook for the current task when
# ``logging.logAsyncioTasks`` is truthy. On this interpreter that hook is not
# reliable enough during bootstrap, and can abort the process with an access
# violation before the app has even configured its own log pipeline.
# Disabling the task-name enrichment before importing structlog makes the
# import side-effect deterministic and keeps the rest of the pipeline untouched.
logging.logAsyncioTasks = False

import structlog  # noqa: E402

from app.core.config import settings  # noqa: E402

# Substrings that mark a value as sensitive. Matched case-insensitively against
# dictionary keys.
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth_header",
    "credential",
    "private_key",
    "access_key",
    "refresh_key",
    "cookie",
    "session_id",
    "jwt",
    "signature",
    "encryption_key",
)

REDACTED = "***redacted***"

# Bearer tokens / API keys that appear inline inside free-text messages.
_INLINE_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"(AIza)[A-Za-z0-9._\-]{10,}"),  # Google API key, legacy shape
    # Google API key, current AI Studio shape (``AQ.`` + base64url payload).
    # Without this, a modern Gemini key pasted into an error string would pass
    # through unredacted.
    re.compile(r"(AQ\.)[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(eyJ[A-Za-z0-9._\-]{10,})"),  # JWT
)

_MAX_VALUE_LENGTH = 4000


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _scrub_text(text: str) -> str:
    for pattern in _INLINE_SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    if len(text) > _MAX_VALUE_LENGTH:
        text = text[:_MAX_VALUE_LENGTH] + f"...[truncated {len(text)} chars]"
    return text


def _scrub(value: Any, *, depth: int = 0) -> Any:
    """Recursively redact sensitive keys and inline secrets."""
    if depth > 6:  # guard against pathological nesting
        return value
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and _is_sensitive(key)
                else _scrub(val, depth=depth + 1)
            )
            for key, val in value.items()
        }
    if isinstance(value, list | tuple | set):
        scrubbed = [_scrub(item, depth=depth + 1) for item in value]
        return type(value)(scrubbed) if isinstance(value, list | tuple) else scrubbed
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return _scrub(dict(event_dict))  # type: ignore[return-value]


def configure_logging() -> None:
    """Install the structlog + stdlib logging configuration.

    Everything -- events logged through structlog *and* records from stdlib
    loggers (uvicorn, sqlalchemy, apscheduler, httpx) -- is rendered by a single
    :class:`structlog.stdlib.ProcessorFormatter` on one root handler. That is what
    makes the redaction guarantee real: a secret logged by a third-party library
    goes through :func:`_redact_processor` on its way out, exactly like one logged
    by AURA's own code.

    Idempotent: safe to call from both the app factory and test fixtures.
    """
    use_json = settings.log_format == "json"
    level = getattr(logging, settings.log_level, logging.INFO)

    # Applied to both native structlog events and foreign stdlib records, so the
    # two are indistinguishable in the output.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        # Deliberately ahead of the redactor: it turns exc_info into a traceback
        # *string*, and a traceback can easily contain a key that appeared in an
        # exception message. Formatting after redaction would smuggle it out.
        structlog.processors.format_exc_info,
        _redact_processor,  # last: nothing may be added to the event after this
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            # Hand the event dict to the formatter instead of rendering here, so
            # native and foreign records share one rendering path.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Must be the stdlib factory: `add_logger_name` above reads `logger.name`,
        # which only a stdlib logger has. Pairing it with WriteLoggerFactory
        # raises AttributeError on the very first log call.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # Brings records from libraries that know nothing about structlog up
            # to the same shape -- including redaction.
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )

    root = logging.getLogger()
    # Replaced, not appended: calling this twice would otherwise duplicate every
    # line for the rest of the process's life.
    root.handlers = [handler]
    root.setLevel(level)

    for noisy, noisy_level in (
        ("uvicorn.access", logging.WARNING if settings.is_production else logging.INFO),
        ("uvicorn.error", logging.INFO),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("sqlalchemy.engine", logging.WARNING),
        ("apscheduler", logging.INFO),
        # The crawler deliberately hits sites that fail; those are handled and
        # reported as scan results, so their tracebacks are not app errors.
        ("asyncio", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)

    # uvicorn installs its own handlers and sets propagate=False, which would keep
    # its output away from the root handler above -- unformatted and, more to the
    # point, unredacted. Clearing them puts uvicorn under the same pipeline.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger.

    Usage::

        log = get_logger(__name__)
        log.info("website_scan_started", website_id=str(website.id), pages=25)
    """
    return structlog.get_logger(name)  # type: ignore[return-value]
