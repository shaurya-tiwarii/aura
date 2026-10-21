"""Alembic runtime environment for AURA.

Five things here are load-bearing and easy to get wrong:

1. **``import app.models``** - Alembic autogenerate can only see tables that are
   attached to ``Base.metadata``, and metadata is only populated as a side effect
   of importing each model module. ``app.models.__init__`` imports all of them,
   so that single import is what makes autogenerate complete rather than empty.

2. **``render_as_batch``** - SQLite cannot ``ALTER`` most things. Batch mode makes
   Alembic emit a create-copy-drop-rename sequence instead, so the same migration
   script applies to both the SQLite fallback and Postgres.

3. **The JSON/JSONB variant** - models use
   ``JSON().with_variant(JSONB, "postgresql")``. Alembic's default renderer
   flattens that to ``sa.JSON()``, which would silently downgrade Postgres
   columns from JSONB to JSON and lose GIN-indexability. :func:`_render_item`
   preserves the variant by rendering the shared ``JSONColumn`` instead.

4. **``func.now()`` server defaults** - Alembic renders a server default by
   compiling it against whichever dialect is connected, so autogenerating on the
   SQLite fallback freezes ``CURRENT_TIMESTAMP`` into the script. That is
   SQLite's spelling; on Postgres it then reads as permanent server-default
   drift. :func:`_render_item` renders ``sa.func.now()`` instead, which compiles
   correctly per dialect.

5. **``UTCDateTime``** - a ``TypeDecorator`` around ``DateTime(timezone=True)``
   that exists purely to normalise Python values; it emits no DDL of its own.
   Alembic would render it as a bare ``UTCDateTime`` requiring an application
   import, so :func:`_render_item` renders the impl instead and migration scripts
   stay replayable without depending on model code.

The database URL comes from application settings, never from ``alembic.ini``, so
migrations always target the same database as the app and no credential is ever
written to a tracked file. Override for a single run with::

    alembic -x db_url=postgresql+psycopg://user:pass@host:5432/aura upgrade head
"""

from __future__ import annotations

import logging
import subprocess
import sys
from logging.config import fileConfig
from typing import Any

from sqlalchemy import JSON, engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import TextClause
from sqlalchemy.sql.functions import now as sql_now
from sqlalchemy.sql.schema import DefaultClause

# Importing the package registers every model on Base.metadata. Without this the
# autogenerate diff would come back empty and silently produce a no-op migration.
import app.models  # noqa: F401  (import side effect is the point)
from alembic import context
from alembic.script.write_hooks import register as register_write_hook
from app.core.config import settings
from app.models.base import Base, UTCDateTime

# Python 3.13's LogRecord task-name probe can access asyncio before the event
# loop/runtime state is ready during Alembic bootstrap. Keep migration logging
# deterministic, matching the application logging configuration.
logging.logAsyncioTasks = False

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


@register_write_hook("ruff")
def _ruff_hook(filename: str, options: dict[str, Any]) -> None:
    """Lint and format a freshly generated migration.

    Registered as a custom hook rather than configured as ``console_scripts`` or
    ``exec`` in ``alembic.ini`` because neither works here: ruff ships a compiled
    binary and registers no ``console_scripts`` entry point, and ``exec`` needs
    ``ruff`` on ``PATH``, which it is not when the virtualenv has not been
    activated in the shell. ``sys.executable -m ruff`` always resolves to the
    interpreter running Alembic, so it works either way.
    """
    for args in (
        ["check", "--fix", "--quiet", filename],
        ["format", "--quiet", filename],
    ):
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "ruff", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            # A formatting failure must not look like a migration failure: the
            # script is already written and valid, it is just unformatted.
            print(
                f"[alembic] ruff {args[0]} skipped: "
                f"{(result.stderr or result.stdout).strip().splitlines()[-1:] or ['not installed']}",
                file=sys.stderr,
            )
            return


def _database_url() -> str:
    """Resolve the target URL: ``-x db_url=...`` wins, else app settings."""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    return override or settings.resolved_database_url


def _is_now_default(obj: Any) -> bool:
    """True when a server default is (or compiled from) SQL ``now()``.

    Two shapes reach the renderer. Autogenerating a *new* table renders the
    metadata's own ``DefaultClause(func.now())``. Comparing against a *reflected*
    column yields the database's text (``CURRENT_TIMESTAMP`` on SQLite,
    ``now()`` on Postgres), so both spellings are matched.
    """
    arg = getattr(obj, "arg", obj)
    if isinstance(arg, sql_now):
        return True
    if isinstance(arg, TextClause):
        text = arg.text.strip().strip("()").strip().lower()
        return text in {"current_timestamp", "now()"}
    return False


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Keep generated scripts dialect-portable.

    Returning ``False`` defers to Alembic's default rendering for everything else.
    """
    if (
        type_ == "type"
        and isinstance(obj, JSON)
        and getattr(obj, "_variant_mapping", None)
    ):
        autogen_context.imports.add("from app.models.base import JSONColumn")
        return "JSONColumn"
    if type_ == "type" and isinstance(obj, UTCDateTime):
        # UTCDateTime is a TypeDecorator: it normalises values in Python and emits
        # exactly the DDL of its impl. Rendering the underlying spelling keeps
        # migration scripts standalone -- a frozen snapshot of the schema should
        # not import application code that may be refactored or renamed later,
        # which would break the ability to replay old revisions.
        return "sa.DateTime(timezone=True)"
    if (
        type_ == "server_default"
        and isinstance(obj, DefaultClause)
        and _is_now_default(obj)
    ):
        return "sa.func.now()"
    return False


def _include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Keep Alembic's diff scoped to tables AURA actually owns.

    Without this, running autogenerate against a Supabase-hosted Postgres would
    try to drop Supabase's own internal tables, and against SQLite it would trip
    over ``sqlite_sequence``.
    """
    if type_ == "table" and reflected and name not in target_metadata.tables:
        return False
    # SIM103 is suppressed below: it would collapse this into a single negated
    # compound return. Two parallel guards, each with its own reason in the
    # docstring above, are easier to extend and to read than
    # `return not (type_ == "table" and ... and name.startswith("sqlite_"))`.
    if type_ == "table" and name is not None and name.startswith("sqlite_"):  # noqa: SIM103
        return False
    return True


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    """Shared configuration for both online and offline runs."""
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        # SQLite cannot ALTER in place; batch mode keeps one script portable.
        render_as_batch=True,
        # Catch column type and default drift, not just added/removed columns.
        compare_type=True,
        compare_server_default=True,
        include_object=_include_object,
        render_item=_render_item,
        # Emit CHECK/UNIQUE/FK names using the models' naming convention so
        # downgrades can find the constraints they need to drop.
        include_schemas=False,
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"},
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    _configure(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        if connection.dialect.name == "sqlite":
            # SQLAlchemy does not turn this on per connection, and batch-mode
            # table rebuilds rely on FK enforcement behaving consistently.
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

        _configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()

        # Alembic marks SQLite as non-transactional DDL, so the
        # ``begin_transaction()`` above is a no-op context manager there. But
        # pysqlite only autocommits DDL -- the ``INSERT INTO alembic_version``
        # that records the revision is DML, so it opens an implicit transaction
        # that nothing commits, and closing the connection rolls it back. The
        # result is the worst kind of half-success: every table created, but the
        # revision unrecorded, so `alembic current` is empty and the next
        # `upgrade head` tries to re-create tables that already exist.
        #
        # On PostgreSQL the block above has already committed, leaving no active
        # transaction, so this is a no-op there.
        if connection.in_transaction():
            connection.commit()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
