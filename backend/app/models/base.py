"""Declarative base, shared column types, and model mixins.

The naming convention below matters more than it looks: without it, Alembic
autogenerate produces anonymous constraint names that differ between Postgres and
SQLite, which makes migrations non-portable and downgrades unreliable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, Dialect, MetaData, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

# Deterministic, dialect-independent constraint names.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# JSONB on Postgres (indexable, queryable); plain JSON elsewhere. Used for
# analysis payloads, category score breakdowns, and connector metadata.
JSONColumn = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")


def utc_now() -> datetime:
    """Timezone-aware current time.

    Always used instead of ``datetime.utcnow()``, which returns a naive datetime
    and silently breaks comparisons against timezone-aware values from Postgres.
    """
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is *always* timezone-aware UTC in Python.

    ``DateTime(timezone=True)`` is not enough, because on SQLite it is a no-op:
    pysqlite has no timestamp type, so the value is stored as a string with the
    offset discarded and read back **naive**. Postgres ``TIMESTAMPTZ`` returns an
    aware value. The result is code that works on one backend and raises
    ``TypeError: can't compare offset-naive and offset-aware datetimes`` on the
    other -- a bug that only ever shows up at runtime, in whichever environment
    was not used during development.

    Since the spec requires both backends to work, the fix belongs in the type
    rather than at each comparison. Every value is normalised to UTC on the way
    in and re-tagged as UTC on the way out, so ORM attributes are aware on both
    backends and the two behave identically.

    The emitted DDL is unchanged (``TIMESTAMP WITH TIME ZONE`` / ``DATETIME``),
    so this is not a schema change and needs no migration.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # Interpreting a naive value as UTC rather than raising: every writer
            # in this codebase uses utc_now(), and a hard failure here would turn
            # a third-party library's naive default into a 500.
            value = value.replace(tzinfo=UTC)
        # Normalising before storage is what makes SQLite's string comparison
        # ordering correct -- mixed offsets would sort lexicographically wrong.
        return value.astimezone(UTC)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for every AURA model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # ClassVar spelled out explicitly. DeclarativeBase already declares
    # ``type_annotation_map`` as a ClassVar, and inheriting that is why a plain
    # assignment works -- but without the annotation here, a type checker (and
    # ruff's RUF012) reads it as a mutable instance attribute shared across
    # subclasses. It is configuration, not per-instance state.
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        # Every `Mapped[datetime]` gets the UTC-normalising type, so a future
        # model cannot reintroduce the naive/aware split by forgetting it.
        datetime: UTCDateTime,
        dict[str, Any]: JSONColumn,
        list[Any]: JSONColumn,
        list[str]: JSONColumn,
        list[dict[str, Any]]: JSONColumn,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    """Opaque UUID primary key.

    UUIDs rather than sequential integers so that resource ids in URLs are not
    guessable and cannot be enumerated across tenants (spec section 20).
    ``Uuid`` maps to native ``uuid`` on Postgres and ``CHAR(32)`` on SQLite.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )


class TimestampMixin:
    """``created_at`` / ``updated_at``, maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utc_now,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        nullable=False,
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )
