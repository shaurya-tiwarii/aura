"""FastAPI application factory.

Run it with::

    uvicorn app.main:app --reload

Structure notes
---------------
* ``create_app()`` is a factory, not a module-level side effect, so tests can
  build an isolated instance. The module-level ``app`` exists only because ASGI
  servers need an import target.
* Startup verifies the database with a real query and logs the capability report.
  It does **not** create tables -- schema changes go through Alembic, so a
  forgotten migration surfaces as a loud error rather than a silently divergent
  schema (``create_all`` would happily invent a table Alembic does not know
  about).
* Middleware order matters and is asserted by the comments below.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.api.router import API_V1_PREFIX, api_router, root_router
from app.core.config import settings
from app.core.database import check_database_connection
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.scheduler import scheduler as publishing_scheduler

log = get_logger(__name__)

DESCRIPTION = """
AURA analyses a small business website, estimates how discoverable it is to
AI-powered search, explains every deduction, and helps fix and publish the
result.

**Two things this API is careful about:**

* The **AI Visibility Score is a heuristic** -- an *estimated AI-search
  readiness* signal built from measurable page properties. It does not predict or
  guarantee ranking or citation in ChatGPT, Gemini, Perplexity or Google, and
  every report carries that disclaimer with it.
* **Every metric is labelled by origin** -- `platform_api`, `user_entered`,
  `derived`, or `demo`. Numbers that came from a simulation are always marked as
  such. Call `GET /api/v1/meta/capabilities` to see exactly which features are
  live on this installation and which credential would enable the rest.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify dependencies at boot and report what this install can actually do."""
    configure_logging()

    report = settings.capability_report()
    db_ok = check_database_connection()

    log.info(
        "startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
        auth_provider=report["auth_provider"],
        database=report["database"],
        database_reachable=db_ok,
        ai_enabled=report["ai"]["enabled"],
        # Named to avoid the substring "token", which the log redactor treats as
        # sensitive -- it would mask this boolean capability flag. Fixing it here
        # rather than loosening the redactor, which should stay fail-closed.
        encryption_configured=report["token_encryption"]["enabled"],
        demo_connectors=sorted(
            name for name, cfg in report["connectors"].items() if cfg["mode"] == "demo"
        ),
    )

    if not db_ok:
        # Not fatal: the process stays up so /health/ready can report the outage
        # and an operator can see *why* rather than watching a container
        # crash-loop with no diagnostics.
        log.error(
            "startup_database_unreachable",
            hint="Check DATABASE_URL and that the server is running.",
        )

    publishing_scheduler.start()
    yield

    # Drain the scan thread pool so an in-flight crawl is cancelled cleanly rather
    # than killed mid-write when the process exits.
    from app.services.scan import shutdown_scan_executor

    shutdown_scan_executor(wait=False)
    publishing_scheduler.stop()
    log.info("shutdown", app=settings.app_name)


def create_app() -> FastAPI:
    """Build a configured application instance."""
    configure_logging()

    app = FastAPI(
        title=f"{settings.app_name} API",
        description=DESCRIPTION,
        version=settings.app_version,
        lifespan=lifespan,
        # Interactive docs are a development and demonstration tool. In
        # production they are disabled so the full schema is not published to
        # anonymous callers.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Middleware runs outermost-last-added. Registering the request-context
    # middleware last therefore makes it the outermost layer, so the request id
    # is bound before CORS or compression touch anything and is available to
    # every error handler.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)
    # Rate limiting sits inside the request-context middleware so the 429
    # handler can include the request id in the standard error envelope.
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    if settings.backend_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            # An explicit origin list, never "*": credentialed requests require
            # it, and a wildcard would let any site call the API with a user's
            # token.
            allow_origins=settings.backend_cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(root_router)
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, str | None]:
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "api": API_V1_PREFIX,
            "docs": None if settings.is_production else "/docs",
            "health": "/health",
            "capabilities": f"{API_V1_PREFIX}/meta/capabilities",
        }

    return app


app = create_app()
