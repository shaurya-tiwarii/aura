"""Rate limiting via slowapi.

Decision: slowapi was a declared dependency but never attached. It is now
attached rather than removed, because credential-stuffing protection on the
auth endpoints and cost protection on the expensive crawl/AI endpoints are
real requirements (the ``RATE_LIMIT_*`` variables were already documented in
``.env.example``).

What is limited:

* auth endpoints (register, login, refresh, forgot/reset password) --
  ``RATE_LIMIT_AUTH_PER_MINUTE`` (default 20/min per IP). Brute-force and
  credential-stuffing protection.
* scan creation -- ``RATE_LIMIT_ANALYSIS_PER_HOUR`` (default 20/hour per IP).
  A crawl is the most expensive thing this API does.
* AI generation endpoints -- ``RATE_LIMIT_AI_PER_HOUR`` (default 60/hour per
  IP). Protects the Gemini free tier.
* everything else -- ``RATE_LIMIT_DEFAULT_PER_MINUTE`` (default 120/min per
  IP) as a backstop.

Set ``RATE_LIMIT_ENABLED=false`` to disable entirely (tests and local dev).

The 429 handler returns the application's standard error envelope rather
than slowapi's plain-text default.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from fastapi import Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

limiter = Limiter(
    key_func=get_remote_address,
    # A global backstop; tighter per-endpoint limits are applied explicitly.
    default_limits=[f"{settings.rate_limit_default_per_minute}/minute"],
    enabled=settings.rate_limit_enabled,
)

AUTH_LIMIT = f"{settings.rate_limit_auth_per_minute}/minute"
ANALYSIS_LIMIT = f"{settings.rate_limit_analysis_per_hour}/hour"
AI_LIMIT = f"{settings.rate_limit_ai_per_hour}/hour"


def limit_route(limit_value: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Drop-in for ``limiter.limit`` that survives PEP 563 string annotations.

    slowapi builds its wrapper with ``functools.wraps``, which does not copy
    ``__globals__``: the wrapper's globals stay slowapi's module namespace.
    Our route modules use ``from __future__ import annotations``, so FastAPI
    resolves the string annotations in the *wrapper's* globals, fails to find
    names like ``RegisterRequest``, and silently degrades every parameter to
    a query parameter (422s everywhere). Resolving the hints eagerly against
    the original function's globals and pinning them as ``__signature__``
    sidesteps the problem without mutating any shared module namespace.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        wrapped = limiter.limit(limit_value)(func)
        try:
            # include_extras: Annotated[..., Depends(...)] aliases (AuthProvider,
            # DbSession, ...) must survive resolution or FastAPI degrades them
            # to plain field types.
            hints = get_type_hints(func, include_extras=True)
        except Exception:  # noqa: BLE001 - fall back to the undecorated wrapper
            return wrapped
        sig = inspect.signature(func)
        params = [
            p.replace(annotation=hints[p.name]) if p.name in hints else p
            for p in sig.parameters.values()
        ]
        wrapped.__signature__ = sig.replace(  # type: ignore[attr-defined]
            parameters=params,
            return_annotation=hints.get("return", sig.return_annotation),
        )
        return wrapped

    return decorator


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "-")
    log.warning(
        "rate_limited",
        path=request.url.path,
        client=get_remote_address(request),
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": {
                "code": "rate_limited",
                "message": "Too many requests. Please wait a moment and try again.",
                "detail": exc.detail,
            },
            "request_id": request_id,
        },
    )
