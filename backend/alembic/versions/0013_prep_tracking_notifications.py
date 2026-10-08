"""prep-time tracking + employee-facing notifications

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-02

- notification_type_enum gains 'prep_overdue_reminder': an in-progress
  certification whose prep has run past its level's business-day target
  (services/notification_sweep.py::_prep_overdue_pass).
- notification_settings.prep_target_business_days: the target per level,
  admin-adjustable like the expiry windows rather than hardcoded.
- notifications.read_at: the portal's Notifications tab marks a
  notification read when the employee opens it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TARGETS = '{"foundational": 10, "associate": 20, "professional": 30, "specialty": 30, "expert": 40}'


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block that
    # later uses the value — give it its own autocommit block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE notification_type_enum ADD VALUE IF NOT EXISTS 'prep_overdue_reminder'")
    op.add_column(
        "notification_settings",
        sa.Column("prep_target_business_days", postgresql.JSONB(), nullable=False,
                  server_default=sa.text(f"'{DEFAULT_TARGETS}'::jsonb")),
    )
    op.add_column("notifications", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "read_at")
    op.drop_column("notification_settings", "prep_target_business_days")
    # Postgres can't drop an enum value; 'prep_overdue_reminder' stays defined.
