"""certification_catalog.short_name + new_certification_added notification type

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-13

Part of the SharePoint-per-provider-file migration (see DEVELOPMENT_LOG.md's
2026-09-13 entry). Excel sheet names are capped at 31 characters and can't
contain \\ / ? * [ ] : — a real certification's full name ("AWS Certified
Solutions Architect - Professional") routinely exceeds that, so every
catalog entry gets a short_name used as its actual sheet name, distinct
from its real name shown everywhere else in this system.

Nullable, not backfilled here — existing rows get their short_name assigned
by the one-time seeding script (scripts/backfill_certification_short_names.py)
that also migrates the existing single tracker workbook into the new
per-provider SharePoint layout, not by this migration. Unique when set
(Postgres allows multiple NULLs through a plain unique constraint, so
not-yet-backfilled rows don't conflict with each other).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "certification_catalog",
        sa.Column("short_name", sa.String(length=31), nullable=True),
    )
    op.create_unique_constraint(
        "uq_certification_catalog_short_name", "certification_catalog", ["short_name"]
    )
    op.execute("ALTER TYPE notification_type_enum ADD VALUE IF NOT EXISTS 'new_certification_added'")


def downgrade() -> None:
    # Postgres can't drop a single enum value — same accepted limitation as
    # every other ADD VALUE migration in this project (e.g. 0007).
    op.drop_constraint("uq_certification_catalog_short_name", "certification_catalog", type_="unique")
    op.drop_column("certification_catalog", "short_name")
