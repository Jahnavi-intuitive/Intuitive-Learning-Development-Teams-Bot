"""employee_skill_profiles + recommendation_feedback — next-certification recommender

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-01

Replaces the practice-learning-path "next certification" frontier
(routers/learning_paths.py /next and /overview, removed) with
services/recommender/. These two main-DB tables hold what the tracker
can't: self-declared experience/expertise/goals, and dismiss feedback.
The partner catalog and partnership requirements live in the portal DB
(app/portal_models.py) — create those with backend/scripts_create_portal_tables.py.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_skill_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "employee_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, unique=True,
        ),
        sa.Column("years_experience", sa.Float(), nullable=True),
        sa.Column("expertise", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("career_goals", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="self_declared"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "recommendation_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "employee_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("certification_name", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_recommendation_feedback_employee_id", "recommendation_feedback", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_recommendation_feedback_employee_id", table_name="recommendation_feedback")
    op.drop_table("recommendation_feedback")
    op.drop_table("employee_skill_profiles")
