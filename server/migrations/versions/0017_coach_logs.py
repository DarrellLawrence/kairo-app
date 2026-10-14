"""v8 block E: the coaching log (sessions, water, packed food, the faith habit)

Revision ID: 0017
Revises: 0016
"""
import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coach_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("module", sa.String(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("what", sa.String(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="chat"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_coach_logs_module", "coach_logs", ["module"])
    op.create_index("ix_coach_logs_day", "coach_logs", ["day"])


def downgrade() -> None:
    op.drop_index("ix_coach_logs_day", "coach_logs")
    op.drop_index("ix_coach_logs_module", "coach_logs")
    op.drop_table("coach_logs")
