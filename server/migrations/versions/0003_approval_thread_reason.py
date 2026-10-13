"""approval cards: chat thread and the model's reason

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.add_column(sa.Column("thread_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reason", sa.Text(), nullable=True))
        batch.create_foreign_key("fk_approvals_thread", "chat_threads", ["thread_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch:
        batch.drop_constraint("fk_approvals_thread", type_="foreignkey")
        batch.drop_column("reason")
        batch.drop_column("thread_id")
