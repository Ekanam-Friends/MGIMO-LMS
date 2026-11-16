"""Оригиналы документов базы знаний и надёжная очередь их обработки."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    PUBLISHED = "published"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"
    TITLES = {
        QUEUED: "В очереди", PROCESSING: "Обрабатывается", PUBLISHED: "Доступен ассистенту",
        NEEDS_REVIEW: "Нужна проверка", ERROR: "Ошибка обработки",
    }


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uploaded_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(String(2000))
    source_key: Mapped[str] = mapped_column(String(64), unique=True)
    revision: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    reviewed_public: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.QUEUED, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
