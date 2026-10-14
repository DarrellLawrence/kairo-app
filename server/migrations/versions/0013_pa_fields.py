"""PA rules: a person's email (for ready-to-send drafts), when a flashcard was last reviewed

Revision ID: 0013
Revises: 0012
"""
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("people") as b:
        b.add_column(sa.Column("email", sa.String(), nullable=True))
    with op.batch_alter_table("flashcards") as b:
        b.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("flashcards") as b:
        b.drop_column("reviewed_at")
    with op.batch_alter_table("people") as b:
        b.drop_column("email")
