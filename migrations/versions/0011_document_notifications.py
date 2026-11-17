"""Адресаты нового документа, подтверждение работника и очередь Telegram."""
from alembic import op
import sqlalchemy as sa

revision = "0011_doc_notices"
down_revision = "0010_documents"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("document_notices",
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id"), primary_key=True),
        sa.Column("suggested_targets", sa.JSON(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("suggestion_at", sa.DateTime(), nullable=True),
        sa.Column("preview_targets", sa.JSON(), nullable=False),
        sa.Column("preview_token", sa.String(64), nullable=True),
        sa.Column("preview_sha256", sa.String(64), nullable=True),
        sa.Column("preview_recipients_hash", sa.String(64), nullable=True),
        sa.Column("preview_count", sa.Integer(), nullable=False),
        sa.Column("preview_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("preview_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True))
    op.create_table("document_recipients",
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("document_notices.document_id"), primary_key=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("students.id"), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_state", sa.String(20), nullable=False),
        sa.Column("telegram_attempts", sa.Integer(), nullable=False),
        sa.Column("telegram_attempted_at", sa.DateTime(), nullable=True),
        sa.Column("telegram_sent_at", sa.DateTime(), nullable=True))
    op.create_index("ix_document_recipients_student_id", "document_recipients", ["student_id"])
    op.create_index("ix_document_recipients_telegram_state", "document_recipients", ["telegram_state"])


def downgrade():
    op.drop_table("document_recipients")
    op.drop_table("document_notices")
