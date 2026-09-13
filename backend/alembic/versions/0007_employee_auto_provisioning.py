"""employees.email/practice nullable + is_directory_synced, for Teams auto-provisioning

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07

Reverts the admin-linking design from migration 0006's aad_object_id
column (never fully shipped — see DEVELOPMENT_LOG.md's 2026-09-07 entry
for why: it made a legitimate employee wait on an admin before the bot
would respond to them at all, which is exactly backwards). Replaces it
with auto-provisioning: an unrecognized Teams identity gets its own
employees row created on first contact, in the same request, with no
admin step.

That means two fields that were always required before genuinely can't
be known at auto-provision time (a Bot Framework activity's `from`
carries only id/name/aadObjectId, nothing else):

- `email` becomes nullable — an auto-provisioned row has none until a
  real directory sync fills it in.
- `practice` becomes nullable — same reason; every practice-dependent
  endpoint (routers/learning_paths.py's /next and /overview) now has to
  treat this as "not set up yet, say so plainly" rather than crash or
  guess a default.

`is_directory_synced` is a new data-provenance flag (not an access
gate): False for an auto-provisioned row, True once a real Entra/HRIS
sync (still a SEAM — doesn't exist yet) confirms and enriches it.
Existing rows predate any of this and are already fully seeded (real
email, real practice) — backfilled to True here, not stubs waiting on
enrichment.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("employees", "email", existing_type=sa.String(255), nullable=True)
    op.alter_column("employees", "practice", nullable=True)
    op.add_column(
        "employees",
        sa.Column("is_directory_synced", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Every row that exists as of this migration was seeded by hand or by
    # script, before auto-provisioning existed — fully populated, not a
    # stub awaiting enrichment.
    op.execute("UPDATE employees SET is_directory_synced = true")

    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'employee_auto_created'")


def downgrade() -> None:
    # Postgres can't drop a single enum value — leaving 'employee_auto_created'
    # in place on downgrade is the standard, accepted limitation here (same
    # as every other ADD VALUE migration would face); it's inert if unused.
    op.drop_column("employees", "is_directory_synced")
    op.alter_column("employees", "practice", nullable=False)
    op.alter_column("employees", "email", existing_type=sa.String(255), nullable=False)
