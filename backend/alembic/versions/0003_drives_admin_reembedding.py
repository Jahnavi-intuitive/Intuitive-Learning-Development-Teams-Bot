"""certification drives, admin auth seam, selective re-embedding

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25

Three unrelated-but-batched additions, all requested together (see
DEVELOPMENT_LOG.md's 2026-08-25 "drives" entry):

1. Certification drives (mandatory campaigns): certification_drives,
   drive_certifications (join), drive_enrollments, plus new enums for
   target scope/metric, drive status, and enrollment status.
2. Admin auth seam for local dev: employees.is_admin (boolean, default
   false) — see services/admin_auth.py.
3. Selective re-embedding: knowledge_base.content_hash (nullable — see
   services/rag_content.py), backfilled for existing rows using pgcrypto's
   sha256() (already enabled by migration 0002) so every pre-existing KB
   row starts with a correct hash instead of NULL (which would otherwise
   force a re-embed of every row on its next unrelated save).

Also extends two existing enums in place (ALTER TYPE ... ADD VALUE):
- catalog_status_enum gains 'deprecated' (soft-delete for certifications
  that certificate_records rows still reference — never hard-deleted).
- audit_action_enum gains 'deleted' (for the rare hard-delete case —
  delete_drive on a zero-enrollment draft).
- kb_status_enum gains 'deprecated' (soft-delete for KB entries, distinct
  from 'pending_review' which means "not yet approved" rather than "was
  active, now retired").
ALTER TYPE ... ADD VALUE cannot run in the same transaction as a statement
that *uses* the new value, but adding the value alone is fine — this
migration doesn't reference any of these new enum values itself.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DRIVE_TARGET_SCOPE_VALUES = ("practice", "department", "organization")
DRIVE_TARGET_METRIC_VALUES = ("count", "percentage")
DRIVE_STATUS_VALUES = ("draft", "active", "closed")
ENROLLMENT_STATUS_VALUES = ("not_started", "in_progress", "completed", "exempted")


def upgrade() -> None:
    # --- 1. Extend existing enums in place -------------------------------
    op.execute("ALTER TYPE catalog_status_enum ADD VALUE IF NOT EXISTS 'deprecated'")
    op.execute("ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'deleted'")
    op.execute("ALTER TYPE kb_status_enum ADD VALUE IF NOT EXISTS 'deprecated'")

    # --- 2. Admin auth seam -----------------------------------------------
    op.add_column(
        "employees", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false())
    )

    # --- 3. Selective re-embedding: content_hash + backfill --------------
    op.add_column("knowledge_base", sa.Column("content_hash", sa.String(64), nullable=True))
    op.execute("UPDATE knowledge_base SET content_hash = encode(sha256(content::bytea), 'hex')")

    # --- 4. Certification drives -------------------------------------------
    for name, values in (
        ("drive_target_scope_enum", DRIVE_TARGET_SCOPE_VALUES),
        ("drive_target_metric_enum", DRIVE_TARGET_METRIC_VALUES),
        ("drive_status_enum", DRIVE_STATUS_VALUES),
        ("enrollment_status_enum", ENROLLMENT_STATUS_VALUES),
    ):
        enum_type = postgresql.ENUM(*values, name=name, create_type=False)
        enum_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "certification_drives",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column(
            "target_scope", postgresql.ENUM(*DRIVE_TARGET_SCOPE_VALUES, name="drive_target_scope_enum", create_type=False), nullable=False
        ),
        sa.Column("target_scope_value", sa.String(255), nullable=False),
        sa.Column(
            "target_metric_type", postgresql.ENUM(*DRIVE_TARGET_METRIC_VALUES, name="drive_target_metric_enum", create_type=False), nullable=False
        ),
        sa.Column("target_value", sa.Float(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("deadline", sa.Date(), nullable=False),
        sa.Column("reminder_cadence", postgresql.JSONB()),
        sa.Column(
            "status", postgresql.ENUM(*DRIVE_STATUS_VALUES, name="drive_status_enum", create_type=False),
            nullable=False, server_default="draft",
        ),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "drive_certifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "drive_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certification_drives.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "certification_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certification_catalog.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("drive_id", "certification_id", name="uq_drive_certification"),
    )
    op.create_index("ix_drive_certifications_drive_id", "drive_certifications", ["drive_id"])

    op.create_table(
        "drive_enrollments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "drive_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certification_drives.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "employee_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "enrollment_status", postgresql.ENUM(*ENROLLMENT_STATUS_VALUES, name="enrollment_status_enum", create_type=False),
            nullable=False, server_default="not_started",
        ),
        sa.Column(
            "certificate_record_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certificate_records.id", ondelete="SET NULL"),
        ),
        sa.Column("exemption_reason", sa.Text()),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("drive_id", "employee_id", name="uq_drive_enrollment"),
    )
    op.create_index("ix_drive_enrollments_drive_id", "drive_enrollments", ["drive_id"])
    op.create_index("ix_drive_enrollments_employee_id", "drive_enrollments", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_drive_enrollments_employee_id", table_name="drive_enrollments")
    op.drop_index("ix_drive_enrollments_drive_id", table_name="drive_enrollments")
    op.drop_table("drive_enrollments")
    op.drop_index("ix_drive_certifications_drive_id", table_name="drive_certifications")
    op.drop_table("drive_certifications")
    op.drop_table("certification_drives")

    op.execute("DROP TYPE IF EXISTS enrollment_status_enum")
    op.execute("DROP TYPE IF EXISTS drive_status_enum")
    op.execute("DROP TYPE IF EXISTS drive_target_metric_enum")
    op.execute("DROP TYPE IF EXISTS drive_target_scope_enum")

    op.drop_column("knowledge_base", "content_hash")
    op.drop_column("employees", "is_admin")

    # Postgres has no ALTER TYPE ... DROP VALUE — the added enum labels
    # ('deprecated', 'deleted') are left in place on downgrade. Harmless:
    # they just become unused labels, same tradeoff as any other
    # additive-enum migration in this project.
