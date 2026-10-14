"""voice calls (Kairo calls the owner through a push)

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("opening", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="ringing"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_calls_status", "calls", ["status"])


def downgrade() -> None:
    op.drop_table("calls")
