"""Fix plan items 26 and 30: how long Claude took to start and to say its first words, and
whether a warm process answered (Run diagnosis shows p50/p95, so the cold start is measured)

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_runs") as b:
        b.add_column(sa.Column("startup_ms", sa.Integer(), nullable=True))
        b.add_column(sa.Column("first_text_ms", sa.Integer(), nullable=True))
        b.add_column(sa.Column("warm", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_runs") as b:
        b.drop_column("warm")
        b.drop_column("first_text_ms")
        b.drop_column("startup_ms")
