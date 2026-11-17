"""Оригиналы документов и очередь индексирования сотрудника.

Revision ID: 0010_documents
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_documents"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uploaded_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("source_url", sa.String(2000), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False, unique=True),
        sa.Column("revision", sa.String(120), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("reviewed_public", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_knowledge_documents_uploaded_by_user_id", "knowledge_documents", ["uploaded_by_user_id"])
    op.create_index("ix_knowledge_documents_status", "knowledge_documents", ["status"])


def downgrade() -> None:
    op.drop_table("knowledge_documents")
