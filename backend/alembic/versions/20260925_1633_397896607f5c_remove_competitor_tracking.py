"""Remove competitor tracking (tables, columns, and feature plumbing).

Revision ID: 397896607f5c
Revises: 3d91c8a7b2e0

Drops the ``competitors`` and ``competitor_reports`` tables and the
``content_opportunities.competitor_id`` column (with its FK), mirroring the
removal of the models, schemas, API routes, and services in the same change.

Notes on CHECK constraints:
- ``website_scans.trigger`` is a plain ``String`` column in the initial schema;
  no CHECK constraint ever listed the ``'competitor'`` trigger value, so there
  is nothing to rewrite here. The ``ScanTrigger.COMPETITOR`` Python enum member
  was removed alongside the code.
- The ``activity_logs.action`` and ``notifications.notification_type`` CHECK
  constraints in the initial schema still name ``'competitor_added'`` /
  ``'competitor_analyzed'``. They are deliberately left untouched: they are
  allow-lists over already-persisted audit rows, and tightening them would
  orphan historical activity/notification data. No code path can insert those
  values anymore (the corresponding enum members are gone).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.models.base import JSONColumn

revision = "397896607f5c"
down_revision = "3d91c8a7b2e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The FK from content_opportunities.competitor_id -> competitors.id has to
    # go before the competitors table can be dropped. Batch mode keeps this
    # working on SQLite, which cannot drop a constrained column in place.
    with op.batch_alter_table("content_opportunities", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_content_opportunities_competitor_id_competitors"),
            type_="foreignkey",
        )
        batch_op.drop_column("competitor_id")

    # competitor_reports references competitors, so it is dropped first.
    with op.batch_alter_table("competitor_reports", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_competitor_reports_created_at"))
        batch_op.drop_index(batch_op.f("ix_competitor_reports_competitor_id"))
        batch_op.drop_index("ix_competitor_reports_competitor_created")
    op.drop_table("competitor_reports")

    with op.batch_alter_table("competitors", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_competitors_created_at"))
        batch_op.drop_index(batch_op.f("ix_competitors_business_id"))
        batch_op.drop_index("ix_competitors_business_active")
    op.drop_table("competitors")


def downgrade() -> None:
    # Recreate the tables exactly as the initial schema defined them.
    op.create_table(
        "competitors",
        sa.Column("business_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("normalized_url", sa.String(length=2048), nullable=False),
        sa.Column("registrable_domain", sa.String(length=255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["business_id"],
            ["businesses.id"],
            name=op.f("fk_competitors_business_id_businesses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitors")),
        sa.UniqueConstraint(
            "business_id", "normalized_url", name="unique_competitor_per_business"
        ),
    )
    with op.batch_alter_table("competitors", schema=None) as batch_op:
        batch_op.create_index(
            "ix_competitors_business_active", ["business_id", "is_active"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_competitors_business_id"), ["business_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_competitors_created_at"), ["created_at"], unique=False
        )

    op.create_table(
        "competitor_reports",
        sa.Column("competitor_id", sa.Uuid(), nullable=False),
        sa.Column("competitor_scan_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_scan_id", sa.Uuid(), nullable=True),
        sa.Column("competitor_seo_score", sa.Float(), nullable=True),
        sa.Column("competitor_ai_visibility_score", sa.Float(), nullable=True),
        sa.Column("own_seo_score", sa.Float(), nullable=True),
        sa.Column("own_ai_visibility_score", sa.Float(), nullable=True),
        sa.Column("seo_score_delta", sa.Float(), nullable=True),
        sa.Column("ai_visibility_score_delta", sa.Float(), nullable=True),
        sa.Column("comparison", JSONColumn, nullable=False),
        sa.Column("gaps", JSONColumn, nullable=False),
        sa.Column("strengths", JSONColumn, nullable=False),
        sa.Column("content_gaps", JSONColumn, nullable=False),
        sa.Column("pages_analyzed", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("partial_reason", sa.String(length=200), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_scan_id"],
            ["website_scans.id"],
            name=op.f("fk_competitor_reports_baseline_scan_id_website_scans"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["competitor_id"],
            ["competitors.id"],
            name=op.f("fk_competitor_reports_competitor_id_competitors"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["competitor_scan_id"],
            ["website_scans.id"],
            name=op.f("fk_competitor_reports_competitor_scan_id_website_scans"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_competitor_reports")),
    )
    with op.batch_alter_table("competitor_reports", schema=None) as batch_op:
        batch_op.create_index(
            "ix_competitor_reports_competitor_created",
            ["competitor_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_competitor_reports_competitor_id"),
            ["competitor_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_competitor_reports_created_at"), ["created_at"], unique=False
        )

    with op.batch_alter_table("content_opportunities", schema=None) as batch_op:
        batch_op.add_column(sa.Column("competitor_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_content_opportunities_competitor_id_competitors"),
            "competitors",
            ["competitor_id"],
            ["id"],
            ondelete="SET NULL",
        )
