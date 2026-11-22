"""Планирование повторов и явное включение новых событий оценок в очередь."""
from alembic import op
import sqlalchemy as sa

revision = "0015_learning_delivery"
down_revision = "0014_learning"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("grade_events", sa.Column("telegram_retry_at", sa.DateTime()))
    op.create_index("ix_grade_events_telegram_retry_at", "grade_events", ["telegram_retry_at"])
    op.add_column("learning_notices", sa.Column("telegram_retry_at", sa.DateTime()))


def downgrade():
    op.drop_column("learning_notices", "telegram_retry_at")
    op.drop_index("ix_grade_events_telegram_retry_at", table_name="grade_events")
    op.drop_column("grade_events", "telegram_retry_at")
