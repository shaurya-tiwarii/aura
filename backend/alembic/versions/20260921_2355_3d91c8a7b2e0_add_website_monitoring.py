"""Add persistent recurring website monitoring settings.

Revision ID: 3d91c8a7b2e0
Revises: 4f51c3d5e28a
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "3d91c8a7b2e0"
down_revision = "4f51c3d5e28a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Batch mode: SQLite cannot add constraints or ALTER columns in place, so
    # every table change goes through the create-copy-drop-rename path. On
    # Postgres the batch block is a plain ALTER TABLE.
    with op.batch_alter_table("websites") as batch_op:
        batch_op.add_column(sa.Column("monitoring_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("monitor_frequency", sa.String(length=16), nullable=False, server_default="weekly"))
        batch_op.add_column(sa.Column("monitor_hour", sa.Integer(), nullable=False, server_default="9"))
        batch_op.add_column(sa.Column("monitor_minute", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monitor_day_of_week", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("monitor_timezone", sa.String(length=64), nullable=False, server_default="UTC"))
        batch_op.add_column(sa.Column("monitor_job_id", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("last_monitor_run_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index("ix_websites_monitoring", ["monitoring_enabled", "monitor_frequency"], unique=False)
        batch_op.create_unique_constraint("uq_websites_monitor_job_id", ["monitor_job_id"])
        batch_op.create_check_constraint("valid_monitor_frequency", "monitor_frequency IN ('daily', 'weekly')")
        batch_op.create_check_constraint("valid_monitor_hour", "monitor_hour >= 0 AND monitor_hour <= 23")
        batch_op.create_check_constraint("valid_monitor_minute", "monitor_minute >= 0 AND monitor_minute <= 59")
        batch_op.create_check_constraint("valid_monitor_day_of_week", "monitor_day_of_week >= 0 AND monitor_day_of_week <= 6")
        batch_op.alter_column("monitoring_enabled", server_default=None)
        batch_op.alter_column("monitor_frequency", server_default=None)
        batch_op.alter_column("monitor_hour", server_default=None)
        batch_op.alter_column("monitor_minute", server_default=None)
        batch_op.alter_column("monitor_day_of_week", server_default=None)
        batch_op.alter_column("monitor_timezone", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("websites") as batch_op:
        batch_op.drop_constraint("valid_monitor_day_of_week", type_="check")
        batch_op.drop_constraint("valid_monitor_minute", type_="check")
        batch_op.drop_constraint("valid_monitor_hour", type_="check")
        batch_op.drop_constraint("valid_monitor_frequency", type_="check")
        batch_op.drop_constraint("uq_websites_monitor_job_id", type_="unique")
        batch_op.drop_index("ix_websites_monitoring")
        for name in [
            "last_monitor_run_at",
            "monitor_job_id",
            "monitor_timezone",
            "monitor_day_of_week",
            "monitor_minute",
            "monitor_hour",
            "monitor_frequency",
            "monitoring_enabled",
        ]:
            batch_op.drop_column(name)
