"""simplify certification_drives: drop metric/target fields, admin no longer enters start_date/org scope value

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02

The "Create a drive" admin form asked for target_metric_type/target_value,
a start_date, and a target_scope_value even for organization-wide drives —
none of that was ever wired to actual behavior: get_drive_detail computes
compliance_pct live from drive_enrollments/certificate_records (see
routers/drives.py), never from target_metric_type/target_value, and
enroll_population ignores target_scope_value entirely when
target_scope == "organization". They were pure admin-entry busywork and,
per the user, a point of confusion for leadership looking at the drives
list. This migration catches the schema up to that:

- Drop target_metric_type / target_value outright (nothing reads them).
- target_scope_value becomes nullable — organization-scoped drives no
  longer carry one.
- start_date becomes nullable — it's no longer admin-entered at creation;
  routers/drives.py now sets it itself, to the actual activation date,
  the moment enroll_population promotes a drive from "draft" to "active".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("certification_drives", "target_metric_type")
    op.drop_column("certification_drives", "target_value")
    op.execute("DROP TYPE IF EXISTS drive_target_metric_enum")

    op.alter_column("certification_drives", "target_scope_value", nullable=True)
    op.alter_column("certification_drives", "start_date", nullable=True)


def downgrade() -> None:
    op.alter_column("certification_drives", "start_date", nullable=False, server_default=sa.func.now())
    op.alter_column("certification_drives", "target_scope_value", nullable=False, server_default="")

    op.execute("CREATE TYPE drive_target_metric_enum AS ENUM ('count', 'percentage')")
    op.add_column(
        "certification_drives",
        sa.Column(
            "target_metric_type",
            sa.Enum("count", "percentage", name="drive_target_metric_enum"),
            nullable=False,
            server_default="percentage",
        ),
    )
    op.add_column(
        "certification_drives",
        sa.Column("target_value", sa.Float(), nullable=False, server_default="100"),
    )
