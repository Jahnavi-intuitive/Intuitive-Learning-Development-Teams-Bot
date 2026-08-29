"""certification prerequisite DAG

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-24

Replaces the single-column, straight-line model of certification ordering
(learning_path_items.sequence_order + .prerequisite_certification_id) with
a real prerequisite graph: certification_prerequisites is an edge table
(certification_id -> prerequisite_certification_id), and
certification_catalog.prerequisite_logic ('any'/'all') governs how
multiple incoming edges into the same certification are evaluated. See
DEVELOPMENT_LOG.md's 2026-08-24 "DAG" entry for why: a straight line can't
express a real branch (one completed cert opening up more than one valid
next certification) or a converging capstone (reachable from more than one
track).

Data migration: every existing learning_path_items row with a
prerequisite_certification_id becomes one certification_prerequisites row
(deduplicated — the same cert can appear in more than one practice's path
with the same prerequisite). Only after that succeeds do the old columns
get dropped.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PREREQUISITE_LOGIC_VALUES = ("any", "all")


def upgrade() -> None:
    # Needed for gen_random_uuid() in the data-migration INSERT below —
    # app-level UUIDs are otherwise generated in Python (see
    # models.uuid_pk), not by Postgres, so this wasn't needed until now.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    prerequisite_logic_enum = postgresql.ENUM(
        *PREREQUISITE_LOGIC_VALUES, name="prerequisite_logic_enum", create_type=False
    )
    prerequisite_logic_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "certification_catalog",
        sa.Column(
            "prerequisite_logic", prerequisite_logic_enum, nullable=False, server_default="any"
        ),
    )

    op.create_table(
        "certification_prerequisites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "certification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certification_catalog.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prerequisite_certification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("certification_catalog.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("track_label", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "certification_id", "prerequisite_certification_id", name="uq_certification_prerequisite_edge"
        ),
    )
    op.create_index(
        "ix_certification_prerequisites_certification_id",
        "certification_prerequisites",
        ["certification_id"],
    )

    # Data migration: fold the old linear prerequisite column into edges,
    # deduplicated on (certification_id, prerequisite_certification_id).
    op.execute(
        """
        INSERT INTO certification_prerequisites
            (id, certification_id, prerequisite_certification_id, created_at, updated_at)
        SELECT gen_random_uuid(), certification_id, prerequisite_certification_id, now(), now()
        FROM (
            SELECT DISTINCT certification_id, prerequisite_certification_id
            FROM learning_path_items
            WHERE prerequisite_certification_id IS NOT NULL
        ) AS dedup
        ON CONFLICT (certification_id, prerequisite_certification_id) DO NOTHING
        """
    )

    op.drop_constraint(
        "learning_path_items_prerequisite_certification_id_fkey", "learning_path_items", type_="foreignkey"
    )
    op.drop_column("learning_path_items", "prerequisite_certification_id")
    op.drop_column("learning_path_items", "sequence_order")


def downgrade() -> None:
    op.add_column(
        "learning_path_items", sa.Column("sequence_order", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "learning_path_items",
        sa.Column("prerequisite_certification_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "learning_path_items_prerequisite_certification_id_fkey",
        "learning_path_items",
        "certification_catalog",
        ["prerequisite_certification_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Best-effort backfill: only single-prerequisite edges can round-trip
    # into the old single-column shape; anything with more than one
    # incoming edge (a real branch/convergence created after this
    # migration) is left NULL here rather than guessed at. sequence_order
    # cannot be reconstructed at all — left at its default of 0 for every
    # row; this downgrade is a safety net, not a lossless inverse.
    op.execute(
        """
        UPDATE learning_path_items li
        SET prerequisite_certification_id = edge.prerequisite_certification_id
        FROM (
            SELECT certification_id, prerequisite_certification_id
            FROM (
                SELECT certification_id, prerequisite_certification_id,
                       count(*) OVER (PARTITION BY certification_id) AS edge_count
                FROM certification_prerequisites
            ) counted
            WHERE edge_count = 1
        ) edge
        WHERE li.certification_id = edge.certification_id
        """
    )

    op.drop_index("ix_certification_prerequisites_certification_id", table_name="certification_prerequisites")
    op.drop_table("certification_prerequisites")
    op.drop_column("certification_catalog", "prerequisite_logic")
    op.execute("DROP TYPE IF EXISTS prerequisite_logic_enum")
