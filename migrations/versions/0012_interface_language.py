"""Язык студенческого кабинета и ответов ассистента."""
from alembic import op
import sqlalchemy as sa

revision = "0012_language"
down_revision = "0011_doc_notices"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("interface_language", sa.String(2), nullable=False, server_default="ru"))
    op.create_check_constraint("ck_users_interface_language", "users", "interface_language IN ('ru', 'en', 'fr', 'zh')")


def downgrade():
    op.drop_constraint("ck_users_interface_language", "users", type_="check")
    op.drop_column("users", "interface_language")
