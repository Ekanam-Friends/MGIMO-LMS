"""Подтверждённые работником адресаты и очередь Telegram для нового документа."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DocumentNotice(Base):
    __tablename__ = "document_notices"
    document_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), primary_key=True)
    suggested_targets: Mapped[list] = mapped_column(JSON, default=list)
    explanation: Mapped[str] = mapped_column(Text, default="")
    suggestion_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    preview_targets: Mapped[list] = mapped_column(JSON, default=list)
    preview_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_recipients_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preview_count: Mapped[int] = mapped_column(Integer, default=0)
    preview_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    preview_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DocumentRecipient(Base):
    __tablename__ = "document_recipients"
    document_id: Mapped[int] = mapped_column(ForeignKey("document_notices.document_id"), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True, index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_state: Mapped[str] = mapped_column(String(20), index=True)
    telegram_attempts: Mapped[int] = mapped_column(Integer, default=0)
    telegram_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    telegram_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
