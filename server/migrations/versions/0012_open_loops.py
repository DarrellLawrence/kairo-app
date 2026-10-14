"""open loops: things Kairo keeps raising until they are done

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "open_loops",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("who", sa.String(), nullable=True),
        sa.Column("who_email", sa.String(), nullable=True),
        sa.Column("what", sa.String(), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due", sa.DateTime(timezone=True), nullable=True),
        sa.Column("urgency", sa.String(), nullable=False, server_default="normal"),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raised", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snoozed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_raised_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("draft_json", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_ref", name="uq_open_loops_source"),
    )
    for col in ("kind", "urgency", "status", "next_at"):
        op.create_index(f"ix_open_loops_{col}", "open_loops", [col])


def downgrade() -> None:
    op.drop_table("open_loops")
