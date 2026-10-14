"""Open loops only for real people: per-mail reply check, senders learned from the owner's closes

Revision ID: 0014
Revises: 0013
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("email_triage") as b:
        b.add_column(sa.Column("personal_reply", sa.Boolean(), nullable=True))
        b.add_column(sa.Column("reply_why", sa.String(), nullable=True))
    op.create_table(
        "reply_senders",
        sa.Column("address", sa.String(), primary_key=True),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("drops", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reply_senders_verdict", "reply_senders", ["verdict"])


def downgrade() -> None:
    op.drop_index("ix_reply_senders_verdict", "reply_senders")
    op.drop_table("reply_senders")
    with op.batch_alter_table("email_triage") as b:
        b.drop_column("reply_why")
        b.drop_column("personal_reply")
