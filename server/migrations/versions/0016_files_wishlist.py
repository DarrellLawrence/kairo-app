"""v8 block B: shared files keep their name and are filed once (hash); wishlists per person

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("captures") as b:
        b.add_column(sa.Column("file_name", sa.String(), nullable=True))
        b.add_column(sa.Column("file_hash", sa.String(), nullable=True))
    op.create_index("ix_captures_file_hash", "captures", ["file_hash"])
    op.create_table(
        "wish_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id"), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("price_chf", sa.Float(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="chat"),
        sa.Column("capture_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wish_items_person_id", "wish_items", ["person_id"])
    op.create_index("ix_wish_items_status", "wish_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_wish_items_status", "wish_items")
    op.drop_index("ix_wish_items_person_id", "wish_items")
    op.drop_table("wish_items")
    op.drop_index("ix_captures_file_hash", "captures")
    with op.batch_alter_table("captures") as b:
        b.drop_column("file_hash")
        b.drop_column("file_name")
