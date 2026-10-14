"""location events (arrive/leave at named places); tasks can be tied to a place

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "location_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("place", sa.String(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_location_events_place", "location_events", ["place"])
    op.create_index("ix_location_events_ts", "location_events", ["ts"])
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("place", sa.String(), nullable=True))
        batch.create_index("ix_tasks_place", ["place"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_place")
        batch.drop_column("place")
    op.drop_table("location_events")
