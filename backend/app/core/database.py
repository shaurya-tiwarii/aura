"""Database engine, session factory, and the FastAPI session dependency.

Dialect handling
----------------
PostgreSQL is the supported production target (spec section 15). SQLite is
supported as a zero-setup fallback so a fresh clone runs without a database
server, and so the test suite can use an isolated in-memory database. The two
dialects differ in ways that matter, and both are handled here rather than being
sprinkled through the models:

* **Connection arguments** - SQLite needs ``check_same_thread=False`` because
  APScheduler jobs and request handlers touch the same connection pool from
  different threads.
* **Foreign keys** - SQLite does *not* enforce foreign keys unless the
  ``PRAGMA foreign_keys=ON`` is issued per connection. Without the listener
  below, the FK constraints the spec requires would silently do nothing on the
  fallback database.
* **Pooling** - Postgres gets a real connection pool with pre-ping (Supabase and
  Render both drop idle connections, which would otherwise surface as random
  ``OperationalError``s on the first query after a quiet period).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


def _engine_kwargs() -> dict[str, Any]:
    """Dialect-appropriate engine options."""
    url = settings.resolved_database_url

    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {
            "connect_args": {"check_same_thread": False},
            "echo": False,
        }
        # An in-memory database exists only for the lifetime of its connection,
        # so every session must share one connection or tables would vanish
        # between calls.
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        return kwargs

    return {
        "pool_pre_ping": True,  # survive dropped idle connections
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 1800,  # recycle every 30 min
        "echo": False,
        "connect_args": {
            "application_name": f"{settings.app_name.lower()}-api",
            "connect_timeout": 10,
        },
    }


def create_db_engine(url: str | None = None) -> Engine:
    """Build an engine. Separate from the module-level singleton for testability."""
    resolved = url or settings.resolved_database_url
    engine = create_engine(resolved, future=True, **_engine_kwargs())

    if resolved.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            """Turn on FK enforcement and use WAL for better concurrency."""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                if ":memory:" not in resolved:
                    cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

    return engine


engine: Engine = create_db_engine()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,  # keeps ORM objects usable after commit in responses
    class_=Session,
    future=True,
)


def get_db() -> Generator[Session]:
    """FastAPI dependency yielding a request-scoped session.

    Rolls back on any exception so a failed request can never leave a partial
    write behind, then always closes the session back into the pool.
    """
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Generator[Session]:
    """Transactional session for code outside the request cycle.

    Used by scheduler jobs and background tasks, which have no FastAPI
    dependency injection available. Commits on success, rolls back on error.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_connection() -> bool:
    """Cheap liveness probe used by ``GET /health``."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001 - a liveness probe must never raise
        # Blind by design: /health has to report "down" for *any* failure mode --
        # auth rejected, DNS gone, socket timeout, driver bug. Narrowing this to
        # SQLAlchemyError would let an unexpected exception type propagate and turn
        # the health endpoint itself into a 500, which is the one thing a probe
        # must not do. The type name is logged so the cause is still diagnosable.
        log.error("database_unreachable", error_type=type(exc).__name__)
        return False
