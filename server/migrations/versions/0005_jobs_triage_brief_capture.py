"""Milestone C: email triage results, morning briefs, capture filing status

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("captures") as batch:
        batch.add_column(sa.Column("source", sa.String(), nullable=False, server_default="shortcut"))
        batch.add_column(sa.Column("status", sa.String(), nullable=False, server_default="new"))
        batch.add_column(sa.Column("filed_as", sa.String(), nullable=True))
        batch.add_column(sa.Column("title", sa.String(), nullable=True))
        batch.add_column(sa.Column("question", sa.Text(), nullable=True))
        batch.add_column(sa.Column("result_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("filed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_captures_status", ["status"])
    op.create_table(
        "email_triage",
        sa.Column("message_id", sa.String(), primary_key=True),
        sa.Column("thread_id", sa.String(), nullable=True),
        sa.Column("sender", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("suspicious", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suspicious_reason", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("triaged_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_email_triage_received_at", "email_triage", ["received_at"])
    op.create_index("ix_email_triage_category", "email_triage", ["category"])
    op.create_index("ix_email_triage_done", "email_triage", ["done"])
    op.create_table(
        "briefs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vault_path", sa.String(), nullable=True),
    )
    op.create_index("ix_briefs_date", "briefs", ["date"], unique=True)


def downgrade() -> None:
    op.drop_table("briefs")
    op.drop_table("email_triage")
    with op.batch_alter_table("captures") as batch:
        batch.drop_index("ix_captures_status")
        for c in ("filed_at", "result_json", "question", "title", "filed_as", "status", "source"):
            batch.drop_column(c)
