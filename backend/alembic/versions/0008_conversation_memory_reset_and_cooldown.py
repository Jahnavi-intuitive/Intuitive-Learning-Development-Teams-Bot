"""conversation_memory: age-based summary reset + summarization retry cooldown

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-13

Two additions to ConversationMemory, found necessary by a direct code
review of the two-tier conversation-memory flow (see DEVELOPMENT_LOG.md's
2026-09-13 entry):

- `turns_folded_since_reset`: before this, a running summary could grow
  without bound across arbitrarily many fold cycles — nothing capped it.
  Rather than hard-truncating the text (explicitly rejected — this bot's
  conversations are short and transactional; content from well beyond
  conversation_summary_max_age_turns back isn't worth carrying forward at
  all), the summary is now discarded outright once this counter crosses
  that threshold. conversation_turns (the raw log) is completely
  unaffected either way — it's never trimmed or deleted, only what feeds
  the live long-term-memory tier is bounded.

- `last_summarize_attempt_at`: before this, a failing summarization call
  (bad Gemini key, quota, transient error) left turns_since_last_summary
  sitting at-or-above its trigger threshold forever, so *every subsequent
  turn* re-attempted the same failing LLM call inline in the request path
  — silent, unbounded latency/cost with no logged signal at all (the only
  failure path in this codebase that logged nothing). Now used to enforce
  conversation_summarize_retry_cooldown_seconds between attempts.

Existing rows default both new columns to 0/NULL — equivalent to "never
folded, never attempted," which is the correct starting state for a
summary that (if present) predates this migration.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversation_memory",
        sa.Column("turns_folded_since_reset", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "conversation_memory",
        sa.Column("last_summarize_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_memory", "last_summarize_attempt_at")
    op.drop_column("conversation_memory", "turns_folded_since_reset")
