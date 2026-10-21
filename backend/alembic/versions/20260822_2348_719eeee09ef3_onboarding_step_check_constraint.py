"""onboarding step check constraint

Revision ID: 719eeee09ef3
Revises: 8886a2e17fe8
Create Date: 2026-08-22 23:48:23.529295+00:00

Adds the CHECK constraint for ``businesses.onboarding_step``, which gained a
Python enum (``OnboardingStep``) when the business API was built. Every other
enum-backed column in this schema is guarded by a CHECK, and the point of that
pattern is that the database and the application cannot disagree about the
allowed values -- a stray step written by a script or a psql session is rejected
rather than silently breaking the resume-onboarding logic.

Two notes:

* Alembic's autogenerate does not diff CHECK constraints, so this file is written
  by hand. Autogenerate was still run first, and came back empty, which is how we
  know there is no *other* drift bundled into this revision.
* ``NULL`` satisfies a CHECK, so businesses that have not started onboarding are
  unaffected and no data migration is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "719eeee09ef3"
down_revision: str | None = "8886a2e17fe8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "ck_businesses_valid_onboarding_step"

# Spelled out rather than imported from ``app.models.enums``: a migration is a
# frozen snapshot, and importing application code would make replaying this
# revision depend on whatever the enum happens to contain later.
CONDITION = (
    "onboarding_step IS NULL OR onboarding_step IN "
    "('business_basics', 'website', 'profile', 'first_scan', 'complete')"
)


def upgrade() -> None:
    # Batch mode because SQLite cannot ALTER a table to add a CHECK; it emits a
    # create-copy-drop-rename instead. On PostgreSQL this compiles to a plain
    # ALTER TABLE ... ADD CONSTRAINT, so one script serves both backends.
    with op.batch_alter_table("businesses") as batch:
        batch.create_check_constraint(op.f(CONSTRAINT_NAME), CONDITION)


def downgrade() -> None:
    with op.batch_alter_table("businesses") as batch:
        batch.drop_constraint(op.f(CONSTRAINT_NAME), type_="check")
