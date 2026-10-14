"""Getting-to-know-you calls: Kairo's open questions and learned facts; call purpose and agenda

Revision ID: 0021
Revises: 0020
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="question"),
        sa.Column("question", sa.String(), nullable=True),
        sa.Column("about", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="chat"),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("asks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_knowledge_items_kind", "knowledge_items", ["kind"])
    op.create_index("ix_knowledge_items_status", "knowledge_items", ["status"])
    with op.batch_alter_table("calls") as b:
        # purpose, not kind: calls.kind (0018) says who started the call (one call budget)
        b.add_column(sa.Column("purpose", sa.String(), nullable=False, server_default="plain"))
        b.add_column(sa.Column("agenda_json", sa.Text(), nullable=True))
        b.create_index("ix_calls_purpose", ["purpose"])


def downgrade() -> None:
    with op.batch_alter_table("calls") as b:
        b.drop_index("ix_calls_purpose")
        b.drop_column("agenda_json")
        b.drop_column("purpose")
    op.drop_index("ix_knowledge_items_status", "knowledge_items")
    op.drop_index("ix_knowledge_items_kind", "knowledge_items")
    op.drop_table("knowledge_items")
