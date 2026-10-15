"""Voice and chat turns, step by step (owner's request 2026-10-15)

Revision ID: 0023
Revises: 0022
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voice_turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("steps_json", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("device_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_voice_turns_ts", "voice_turns", ["ts"])
    op.create_index("ix_voice_turns_version", "voice_turns", ["version"])
    op.create_index("ix_voice_turns_path", "voice_turns", ["path"])


def downgrade() -> None:
    op.drop_table("voice_turns")
