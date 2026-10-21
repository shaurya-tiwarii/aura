"""enforce the recommendation reconcile deduplication key

Revision ID: 4f51c3d5e28a
Revises: 719eeee09ef3
Create Date: 2026-08-27 01:00:00+00:00

``Recommendation`` is a business-owned, persistent action rather than a
per-scan snapshot.  The recommendation service therefore reconciles on the
pair ``(business_id, rule_id)``.  The model has always described that pair as
the deduplication key, but without a unique constraint two concurrent requests
could both fail to see the other transaction's new row and insert duplicates.

The constraint keeps the database and service contract aligned.  It has no data
rewrite: existing databases created by the earlier stages contain no generated
recommendations, and a deployment that does have duplicate legacy rows must
resolve that ambiguity before claiming a deterministic action history.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "4f51c3d5e28a"
down_revision: str | None = "719eeee09ef3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "uq_recommendations_business_rule"


def upgrade() -> None:
    # Batch mode keeps this portable: SQLite recreates the table while Postgres
    # uses an ALTER TABLE, so both supported databases enforce the same key.
    with op.batch_alter_table("recommendations") as batch:
        batch.create_unique_constraint(
            op.f(CONSTRAINT_NAME), ["business_id", "rule_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("recommendations") as batch:
        batch.drop_constraint(op.f(CONSTRAINT_NAME), type_="unique")
