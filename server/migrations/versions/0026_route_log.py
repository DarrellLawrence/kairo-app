"""G11, the model router: one row per routing decision, with its reason (More -> Usage)

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "route_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("purpose", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("hits", sa.String(), nullable=True),
        sa.Column("route", sa.String(), nullable=False),
        sa.Column("tier", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("fallback_from", sa.String(), nullable=True),
    )
    op.create_index("ix_route_log_ts", "route_log", ["ts"])
    op.create_index("ix_route_log_purpose", "route_log", ["purpose"])
    op.create_index("ix_route_log_route", "route_log", ["route"])


def downgrade() -> None:
    op.drop_table("route_log")
