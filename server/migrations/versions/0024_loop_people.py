"""Open loops done right (owner's request 2026-10-15): the mail a loop came from, the people on it,
the action Kairo picked, and the sanity check's verdict

Revision ID: 0024
Revises: 0023
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("open_loops") as b:
        b.add_column(sa.Column("mail_ref", sa.String(), nullable=True))
        b.add_column(sa.Column("people_json", sa.Text(), nullable=True))
        b.add_column(sa.Column("action", sa.String(), nullable=True))
        b.add_column(sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("check_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("open_loops") as b:
        for c in ("check_json", "checked_at", "action", "people_json", "mail_ref"):
            b.drop_column(c)
