"""llm_call_log table — durable copy of every per-call LLM API log row

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15

Direct request: the JSONL file services/llm_call_log.py writes to
(backend/logs/llm_calls.jsonl) doesn't survive on Render — free web
services have no persistent disk, so it's wiped on every restart/
redeploy/idle spin-down there. This table is the durable version of the
same rows; see models.py::LlmCallLog for the full field-by-field
rationale.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_call_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("agent_id", sa.String(length=255), nullable=False),
        sa.Column("request_id", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error_type", sa.String(length=255), nullable=True),
        sa.Column("endpoint", sa.String(length=255), nullable=False),
        sa.Column("env", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_llm_call_log_call_timestamp", "llm_call_log", ["call_timestamp"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_log_call_timestamp", table_name="llm_call_log")
    op.drop_table("llm_call_log")
