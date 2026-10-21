"""Application error types and HTTP exception handlers.

Every error the API returns uses one envelope::

    {
      "error": {
        "code": "website_unreachable",
        "message": "We could not reach that website.",
        "detail": "Connection timed out after 15s",   // optional, safe to show
        "hint": "Check the URL is correct and publicly accessible.",  // optional
        "fields": {"url": "must be http or https"}    // validation errors only
      },
      "request_id": "..."
    }

Spec section 20 forbids leaking internal stack traces to clients. Unexpected
exceptions are therefore logged in full server-side and reduced to a generic
message with a request id the user can quote. ``AuraError`` subclasses are
deliberately user-facing: their messages are written for a non-technical
business owner (spec section 26).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

log = get_logger(__name__)


class AuraError(Exception):
    """Base class for all deliberate, user-facing application errors."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "error"
    message: str = "Something went wrong."
    hint: str | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: str | None = None,
        hint: str | None = None,
        code: str | None = None,
        status_code: int | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.detail = detail
        self.hint = hint or self.hint
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.fields = fields
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            error["detail"] = self.detail
        if self.hint:
            error["hint"] = self.hint
        if self.fields:
            error["fields"] = self.fields
        return {"error": error}


# --------------------------------------------------------------------- auth
class AuthenticationError(AuraError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "not_authenticated"
    message = "Please sign in to continue."


class InvalidCredentialsError(AuraError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "invalid_credentials"
    message = "That email and password combination is not correct."


class PermissionDeniedError(AuraError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have access to this resource."


class EmailAlreadyRegisteredError(AuraError):
    status_code = status.HTTP_409_CONFLICT
    code = "email_already_registered"
    message = "An account with that email already exists."
    hint = "Try signing in, or use the forgot-password link."


# ---------------------------------------------------------------- resources
class NotFoundError(AuraError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "We could not find what you were looking for."


class ConflictError(AuraError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "That action conflicts with the current state."


class ValidationFailedError(AuraError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_failed"
    message = "Some of the information provided is not valid."


# ------------------------------------------------------------------ crawler
class InvalidURLError(AuraError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_url"
    message = "That does not look like a valid website address."
    hint = "Use a full address including https://, for example https://example.com"


class BlockedURLError(AuraError):
    """Raised by the SSRF guard for private, loopback, or non-HTTP targets."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "blocked_url"
    message = "That address cannot be analysed."
    hint = "AURA only analyses public websites reachable over http or https."


class WebsiteUnreachableError(AuraError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "website_unreachable"
    message = "We could not reach that website."
    hint = "Check the address is correct and the site is publicly accessible."


class WebsiteTimeoutError(AuraError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "website_timeout"
    message = "That website took too long to respond."
    hint = "The site may be slow or temporarily down. Try again in a few minutes."


class CrawlerBlockedError(AuraError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "crawler_blocked"
    message = "That website does not allow automated analysis."
    hint = (
        "The site's robots.txt disallows crawling. You can still add pages "
        "manually or analyse a different URL."
    )


# ----------------------------------------------------------------------- ai
class AIUnavailableError(AuraError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "ai_unavailable"
    message = "AI features are not configured on this installation."
    hint = "Add a GEMINI_API_KEY to the environment to enable content generation."


class AIGenerationError(AuraError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ai_generation_failed"
    message = "The AI model could not complete that request."
    hint = "Try again, or simplify the request."


class AIRateLimitError(AuraError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "ai_rate_limited"
    message = "The AI service is rate limited right now."
    hint = "Wait a minute and try again. The free tier allows a limited number of requests."


class RateLimitedError(AuraError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please slow down."


# --------------------------------------------------------------- publishing
class ConnectorNotConfiguredError(AuraError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "connector_not_configured"
    message = "That platform is not connected yet."
    hint = (
        "Connect the account from Settings, or use demo mode to preview the workflow."
    )


class ConnectorAuthError(AuraError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "connector_auth_failed"
    message = "The connection to that platform is no longer valid."
    hint = "Reconnect the account from the Publishing page."


class PublishFailedError(AuraError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "publish_failed"
    message = "Publishing failed."
    hint = "The post has been kept as a draft so nothing is lost. You can retry it."

    def __init__(self, message: str | None = None, **kwargs) -> None:
        # The id of the Post row carrying the failed attempt, when the failure
        # happened after the row was created. Lets the route commit the failed
        # record instead of rolling it back with the exception.
        self.post_id: uuid.UUID | None = kwargs.pop("post_id", None)
        super().__init__(message, **kwargs)


# ----------------------------------------------------------------- handlers
def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def _respond(
    request: Request, status_code: int, payload: dict[str, Any]
) -> JSONResponse:
    payload = {**payload, "request_id": _request_id(request)}
    return JSONResponse(status_code=status_code, content=payload)


async def aura_error_handler(request: Request, exc: AuraError) -> JSONResponse:
    # 5xx means something on our side misbehaved: log loudly. 4xx is normal
    # client-driven traffic and only worth debug-level detail.
    logger = log.bind(
        request_id=_request_id(request),
        path=request.url.path,
        code=exc.code,
    )
    if exc.status_code >= 500:
        logger.error("request_failed", message=exc.message, detail=exc.detail)
    else:
        logger.debug("request_rejected", message=exc.message, detail=exc.detail)
    return _respond(request, exc.status_code, exc.to_payload())


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Flatten Pydantic errors into ``{field: message}`` for inline form display."""
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", ()) if part != "body"]
        key = ".".join(location) or "body"
        fields.setdefault(key, error.get("msg", "is not valid"))

    payload = {
        "error": {
            "code": "validation_failed",
            "message": "Some of the information provided is not valid.",
            "fields": fields,
        }
    }
    log.debug(
        "validation_failed",
        request_id=_request_id(request),
        path=request.url.path,
        fields=fields,
    )
    return _respond(request, status.HTTP_422_UNPROCESSABLE_ENTITY, payload)


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalise framework-raised HTTPExceptions into the AURA envelope."""
    code_map = {
        400: "bad_request",
        401: "not_authenticated",
        403: "permission_denied",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        429: "rate_limited",
    }
    code = code_map.get(exc.status_code, "http_error")
    detail = exc.detail if isinstance(exc.detail, str) else None
    payload = {
        "error": {
            "code": code,
            "message": detail or "The request could not be completed.",
        }
    }
    return _respond(request, exc.status_code, payload)


async def database_error_handler(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    """Database failures: log the real cause, tell the user nothing internal."""
    log.error(
        "database_error",
        request_id=_request_id(request),
        path=request.url.path,
        error_type=type(exc).__name__,
        exc_info=exc,
    )
    payload = {
        "error": {
            "code": "database_error",
            "message": "We had trouble saving your data. Please try again.",
        }
    }
    return _respond(request, status.HTTP_503_SERVICE_UNAVAILABLE, payload)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort. Full traceback to the logs, opaque message to the client."""
    log.error(
        "unhandled_exception",
        request_id=_request_id(request),
        path=request.url.path,
        error_type=type(exc).__name__,
        exc_info=exc,
    )
    payload = {
        "error": {
            "code": "internal_error",
            "message": "Something went wrong on our end. Please try again.",
            "hint": "If this keeps happening, quote the request id shown below.",
        }
    }
    return _respond(request, status.HTTP_500_INTERNAL_SERVER_ERROR, payload)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every handler. Called once from the app factory."""
    app.add_exception_handler(AuraError, aura_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, database_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
