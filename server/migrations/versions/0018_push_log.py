"""Fix plan items 20-21: the push log (one notification boss: sent / held / dropped, and why) and who
started a call (one Kairo call a day; the owner's own calls never count)

Revision ID: 0018
Revises: 0017
"""
import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("url", sa.String(), nullable=False, server_default="/"),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("why", sa.String(), nullable=False, server_default=""),
        sa.Column("send_after", sa.DateTime(timezone=True), nullable=True),
    )
    for col in ("ts", "source", "key", "status"):
        op.create_index(f"ix_push_log_{col}", "push_log", [col])
    with op.batch_alter_table("calls") as b:
        b.add_column(sa.Column("kind", sa.String(), nullable=False, server_default="kairo"))
    op.execute("UPDATE calls SET kind='owner' WHERE reason IN ('You called Kairo', 'Test call')")


def downgrade() -> None:
    with op.batch_alter_table("calls") as b:
        b.drop_column("kind")
    for col in ("ts", "source", "key", "status"):
        op.drop_index(f"ix_push_log_{col}", "push_log")
    op.drop_table("push_log")
