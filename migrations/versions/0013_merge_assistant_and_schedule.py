"""Объединить независимые ветви расписания и ассистента без потери применённых миграций."""

revision = "0013_merge"
down_revision = ("0010", "0012_language")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
