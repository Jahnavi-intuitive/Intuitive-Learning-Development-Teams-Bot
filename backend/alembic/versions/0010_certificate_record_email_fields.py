"""certificate_records: employee_email, manager_email, practice_director_email

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-13

Part of the tracker header rework (see DEVELOPMENT_LOG.md's 2026-09-13
entry) — the sheet header row grew three new columns (Email ID, Manager
Mail, PD Mail) that need somewhere real to land, not just be read and
discarded. employee_email also becomes an alternate, more reliable match
key for services/excel_sync.py::resolve_employee (name matching alone is
ambiguous; email isn't) — tried first, falling back to name if absent or
unmatched.

All three nullable: a record created directly through the submission form
(not via sync) has no sheet row to pull these from at all.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("certificate_records", sa.Column("employee_email", sa.String(length=255), nullable=True))
    op.add_column("certificate_records", sa.Column("manager_email", sa.String(length=255), nullable=True))
    op.add_column(
        "certificate_records", sa.Column("practice_director_email", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("certificate_records", "practice_director_email")
    op.drop_column("certificate_records", "manager_email")
    op.drop_column("certificate_records", "employee_email")
