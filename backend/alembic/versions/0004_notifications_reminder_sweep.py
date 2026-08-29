"""notifications + notification_settings for the reminder sweep job

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-25

Adds the schema for a scheduled sweep job (APScheduler locally; Cloud
Scheduler + Cloud Functions later per the BRD's own proposed stack) rather
than a per-record task queue — see DEVELOPMENT_LOG.md's 2026-08-25
"reminder sweep" entry for the reasoning.

- `notification_settings`: a single adjustable row (org-wide expiry
  reminder windows + retry cap) instead of hardcoded constants, seeded
  here with the PLACEHOLDER values the user themselves gave as an example
  ([90, 60, 30, 7] days, retry_cap=3) — explicitly not final; flagged to
  the user to confirm the real windows.
- `notifications`: one row per reminder/escalation the sweep decided to
  send. `dedupe_key` carries the actual idempotency guarantee via its
  unique constraint — `INSERT ... ON CONFLICT (dedupe_key) DO NOTHING` is
  what makes re-running the sweep for the same day a no-op, not
  application-level "have I already sent this" logic.

certification_drives.reminder_cadence (JSONB, already exists since
migration 0003) needs no migration to change shape — Postgres JSONB has
no fixed schema. Its new shape ({"reminder_days": [...], "escalation":
[{"threshold_days": N, "escalate_to": "manager"|"practice_director"}]})
is enforced at the application layer (schemas.py) only.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NOTIFICATION_TYPE_VALUES = ("drive_reminder", "drive_escalation", "cert_expiry_reminder")
NOTIFICATION_CHANNEL_VALUES = ("teams",)
NOTIFICATION_STATUS_VALUES = ("pending", "sent", "failed")
ESCALATION_LEVEL_VALUES = ("employee", "manager", "practice_director")


def upgrade() -> None:
    for name, values in (
        ("notification_type_enum", NOTIFICATION_TYPE_VALUES),
        ("notification_channel_enum", NOTIFICATION_CHANNEL_VALUES),
        ("notification_status_enum", NOTIFICATION_STATUS_VALUES),
        ("escalation_level_enum", ESCALATION_LEVEL_VALUES),
    ):
        enum_type = postgresql.ENUM(*values, name=name, create_type=False)
        enum_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "notification_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("expiry_reminder_days", postgresql.JSONB(), nullable=False),
        sa.Column("retry_cap", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column(
            "notification_type",
            postgresql.ENUM(*NOTIFICATION_TYPE_VALUES, name="notification_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "recipient_employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "channel",
            postgresql.ENUM(*NOTIFICATION_CHANNEL_VALUES, name="notification_channel_enum", create_type=False),
            nullable=False,
            server_default="teams",
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "related_drive_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certification_drives.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "related_certificate_record_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certificate_records.id", ondelete="CASCADE"),
        ),
        sa.Column("threshold_days", sa.Integer()),
        sa.Column(
            "escalation_level",
            postgresql.ENUM(*ESCALATION_LEVEL_VALUES, name="escalation_level_enum", create_type=False),
        ),
        sa.Column(
            "status",
            postgresql.ENUM(*NOTIFICATION_STATUS_VALUES, name="notification_status_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("is_test_data", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_dedupe_key"),
    )
    op.create_index("ix_notifications_status", "notifications", ["status"])
    op.create_index("ix_notifications_recipient", "notifications", ["recipient_employee_id"])

    # Exactly one settings row, seeded with example numbers
    # as an explicit placeholder — NOT presented as final; see the
    # DEVELOPMENT_LOG.md entry and the chat response for this work.
    op.execute(
        """
        INSERT INTO notification_settings (id, expiry_reminder_days, retry_cap, created_at, updated_at)
        SELECT gen_random_uuid(), '[90, 60, 30, 7]'::jsonb, 3, now(), now()
        """
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_recipient", table_name="notifications")
    op.drop_index("ix_notifications_status", table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("notification_settings")
    op.execute("DROP TYPE IF EXISTS escalation_level_enum")
    op.execute("DROP TYPE IF EXISTS notification_status_enum")
    op.execute("DROP TYPE IF EXISTS notification_channel_enum")
    op.execute("DROP TYPE IF EXISTS notification_type_enum")
