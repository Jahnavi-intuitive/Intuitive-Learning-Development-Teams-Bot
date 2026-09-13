"""employees.aad_object_id + conversation_turns/conversation_memory for the Teams bot

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02

Two independent additions for the Teams integration:

- `employees.aad_object_id`: nullable, unique. The Bot Framework activity's
  `from.aadObjectId` becomes the real identity join key once the bot runs
  inside actual Teams — nullable because the local test roster has no real
  AAD object IDs yet (see models.py's Employee docstring SEAM note).
- `conversation_turns` / `conversation_memory`: two-tier conversation
  memory backing a custom ADK SessionService/MemoryService, so a reopened
  Teams chat has real continuity instead of ADK's in-memory default losing
  everything on restart. `conversation_turns` is the append-only raw log
  (tier 1); `conversation_memory` is one row per employee holding a running
  LLM-folded summary (tier 2) plus how many raw turns have accumulated
  since the last fold. See services/conversation_memory.py for the actual
  read/summarize logic — this migration only adds the storage.

Chained onto revision 0005 to match what's actually applied to the local
dev database right now (`alembic_version` already reads 0005) even though
0005's own file is not yet committed to git — see the 2026-09-02
DEVELOPMENT_LOG.md entry for why, and the explicit instruction not to
commit any of this yet.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONVERSATION_ROLE_VALUES = ("user", "agent")


def upgrade() -> None:
    op.add_column("employees", sa.Column("aad_object_id", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_employees_aad_object_id", "employees", ["aad_object_id"])

    conversation_role_enum = postgresql.ENUM(*CONVERSATION_ROLE_VALUES, name="conversation_role_enum")
    conversation_role_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "conversation_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(*CONVERSATION_ROLE_VALUES, name="conversation_role_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_conversation_turns_employee", "conversation_turns", ["employee_id", "created_at"])

    op.create_table(
        "conversation_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "employee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("summary_text", sa.Text()),
        sa.Column("last_summarized_at", sa.DateTime(timezone=True)),
        sa.Column("turns_since_last_summary", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("employee_id", name="uq_conversation_memory_employee"),
    )


def downgrade() -> None:
    op.drop_table("conversation_memory")
    op.drop_index("ix_conversation_turns_employee", table_name="conversation_turns")
    op.drop_table("conversation_turns")
    op.execute("DROP TYPE IF EXISTS conversation_role_enum")
    op.drop_constraint("uq_employees_aad_object_id", "employees", type_="unique")
    op.drop_column("employees", "aad_object_id")
