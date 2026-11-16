"""Покрытие семестра по каждой успешно загруженной группе."""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_schedule_coverage",
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("study_groups.id"), primary_key=True),
        sa.Column("period_start", sa.Date(), primary_key=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("import_runs.id"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("group_schedule_coverage")
