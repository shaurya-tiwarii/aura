#!/usr/bin/env python3
"""Container entrypoint for the AURA backend.

Runs, in order:

1. Wait for Postgres to accept TCP connections (parsed from ``DATABASE_URL``).
   Skipped silently when ``DATABASE_URL`` is not a Postgres URL.
2. ``alembic upgrade head`` so the schema always matches the code before the
   server starts accepting traffic.
3. ``exec`` the command passed as arguments (the image ``CMD``) -- normally
   ``uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2``. ``exec``
   replaces this process so signals (SIGTERM from the orchestrator) reach
   uvicorn directly instead of a shell wrapper.

The scheduler leader election (see ``app/scheduler/service.py``) guarantees
that only one of the uvicorn worker processes runs the APScheduler jobs, so
raising the worker count here does not duplicate scheduled publishing or
monitoring runs.
"""

from __future__ import annotations

import os
import socket
import sys
import time
from urllib.parse import urlparse

# Kept in sync with the image CMD. If the CMD is overridden without an
# explicit worker count, this default still applies.
DEFAULT_WORKERS = "2"
DB_WAIT_TIMEOUT_SECONDS = 90


def _postgres_target() -> tuple[str, int] | None:
    """Return ``(host, port)`` for a Postgres ``DATABASE_URL``, else ``None``."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.scheme.startswith("postgres"):
        return None
    return parsed.hostname or "localhost", parsed.port or 5432


def wait_for_postgres(timeout: int = DB_WAIT_TIMEOUT_SECONDS) -> None:
    target = _postgres_target()
    if target is None:
        print(
            "entrypoint: DATABASE_URL is not a Postgres URL; skipping wait-for-db",
            flush=True,
        )
        return
    host, port = target
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=5):
                print(
                    f"entrypoint: postgres reachable at {host}:{port}", flush=True
                )
                return
        except OSError as exc:
            if time.monotonic() >= deadline:
                print(
                    f"entrypoint: timed out after {timeout}s waiting for "
                    f"postgres at {host}:{port}: {exc}",
                    flush=True,
                )
                sys.exit(1)
            time.sleep(2)


def run_migrations() -> None:
    print("entrypoint: running alembic upgrade head", flush=True)
    # The Dockerfile sets WORKDIR /app/backend, but be explicit so this also
    # works when invoked from anywhere else.
    backend_dir = os.environ.get("AURA_BACKEND_DIR", "/app/backend")
    if backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    # This project ships no alembic.ini on purpose (no credentials in tracked
    # files); env.py reads the URL from app settings, so run the migration
    # programmatically with a Config that only sets the script location.
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(backend_dir, "alembic"))
    command.upgrade(cfg, "head")


def main() -> None:
    wait_for_postgres()
    run_migrations()
    cmd = sys.argv[1:] or [
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        DEFAULT_WORKERS,
    ]
    print(f"entrypoint: exec {' '.join(cmd)}", flush=True)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
