"""Язык студенческого кабинета и ответов ассистента."""
from alembic import op
import sqlalchemy as sa

revision = "0012_language"
down_revision = "0011_doc_notices"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("interface_language", sa.String(2), nullable=False, server_default="ru"))
        batch.create_check_constraint("ck_users_interface_language", "interface_language IN ('ru', 'en', 'fr', 'zh')")


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("ck_users_interface_language", type_="check")
        batch.drop_column("interface_language")
