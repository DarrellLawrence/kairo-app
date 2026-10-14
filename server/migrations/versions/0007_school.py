"""school: grade titles, flashcards (Leitner)

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("grades") as batch:
        batch.add_column(sa.Column("title", sa.String(), nullable=True))
    op.create_table(
        "flashcards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=True),
        sa.Column("front", sa.Text(), nullable=False),
        sa.Column("back", sa.Text(), nullable=False),
        sa.Column("box", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("due", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_flashcards_subject", "flashcards", ["subject"])
    op.create_index("ix_flashcards_due", "flashcards", ["due"])


def downgrade() -> None:
    op.drop_table("flashcards")
    with op.batch_alter_table("grades") as batch:
        batch.drop_column("title")
