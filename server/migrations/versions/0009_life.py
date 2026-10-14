"""phase 3 life data: transactions, health days, people, chores, shopping list

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="CHF"),
        sa.Column("merchant", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=False, server_default="other"),
        sa.Column("source", sa.String(), nullable=False, server_default="wallet"),
        sa.Column("card", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("dedupe", sa.String(), nullable=False, unique=True),
    )
    op.create_index("ix_transactions_ts", "transactions", ["ts"])
    op.create_index("ix_transactions_category", "transactions", ["category"])
    op.create_table(
        "health_days",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("sleep_h", sa.Float(), nullable=True),
        sa.Column("resting_hr", sa.Integer(), nullable=True),
        sa.Column("active_kcal", sa.Integer(), nullable=True),
        sa.Column("workout_min", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("relation", sa.String(), nullable=True),
        sa.Column("birthday", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_contact", sa.Date(), nullable=True),
        sa.Column("contact_every_days", sa.Integer(), nullable=True),
    )
    op.create_table(
        "chores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("every_days", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("last_done", sa.Date(), nullable=True),
    )
    op.create_table(
        "shopping_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="app"),
    )
    op.create_index("ix_shopping_items_done", "shopping_items", ["done"])


def downgrade() -> None:
    for t in ("shopping_items", "chores", "people", "health_days", "transactions"):
        op.drop_table(t)
