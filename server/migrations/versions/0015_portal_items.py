"""School portal (OLAT) items: files saved, pages changed, timetable

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("site", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("course", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("vault_path", sa.String(), nullable=True),
        sa.Column("digest", sa.String(), nullable=True),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("briefed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_portal_items_site", "portal_items", ["site"])
    op.create_index("ix_portal_items_key", "portal_items", ["key"])
    op.create_index("ix_portal_items_briefed", "portal_items", ["briefed"])


def downgrade() -> None:
    op.drop_index("ix_portal_items_briefed", "portal_items")
    op.drop_index("ix_portal_items_key", "portal_items")
    op.drop_index("ix_portal_items_site", "portal_items")
    op.drop_table("portal_items")
