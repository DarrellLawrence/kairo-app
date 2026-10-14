"""Fix plan item 31: how long the owner waited (speech to text, Kairo's voice, a call turn), for the
p50/p95 in More -> System

Revision ID: 0020
Revises: 0019
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "latency",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ms", sa.Integer(), nullable=False),
    )
    op.create_index("ix_latency_ts", "latency", ["ts"])
    op.create_index("ix_latency_kind", "latency", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_latency_kind", "latency")
    op.drop_index("ix_latency_ts", "latency")
    op.drop_table("latency")
