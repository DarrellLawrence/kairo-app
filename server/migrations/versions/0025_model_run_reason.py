"""Owner's rule 2026-10-19 (calls on Haiku unless it's a real judgment call): why an owner turn didn't run
on Haiku (the judgment phrase, money, settings, Haiku's hand-over), shown under More -> Usage

Revision ID: 0025
Revises: 0024
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("model_runs") as b:
        b.add_column(sa.Column("reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("model_runs") as b:
        b.drop_column("reason")
