"""reviews get a lifecycle (open/done) with pre-filled data and answers; onboarding answers

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reviews") as batch:
        batch.add_column(sa.Column("period_end", sa.Date(), nullable=True))
        batch.add_column(sa.Column("status", sa.String(), nullable=False, server_default="open"))
        batch.add_column(sa.Column("prefill_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("answers_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_reviews_status", ["status"])
        batch.create_unique_constraint("uq_reviews_kind_period", ["kind", "period_start"])
    op.create_table(
        "onboarding_answers",
        sa.Column("question_id", sa.String(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("onboarding_answers")
    with op.batch_alter_table("reviews") as batch:
        batch.drop_constraint("uq_reviews_kind_period", type_="unique")
        batch.drop_index("ix_reviews_status")
        for c in ("completed_at", "created_at", "answers_json", "prefill_json", "status", "period_end"):
            batch.drop_column(c)
