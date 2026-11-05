"""Привязка Telegram-бота к студенту и его настройки (решение docs/decisions/0005-telegram-bot.md).

Студент подключает бота из профиля кабинета: кабинет выдаёт одноразовый код, бот получает
его в `/start <код>`. Пароль в бот не вводится, и бот знает студента только через эту
таблицу — по telegram id отправителя.

Код хранится SHA-256-хэшем, как токены сессий: утечка таблицы не даёт привязать чужой
кабинет к своему Telegram.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), unique=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime)

    feed_token: Mapped[str] = mapped_column(String(64), unique=True)
    """Секрет в ссылке-подписке на календарь. Перевыпускается в настройках; умирает с /logout."""

    digest_at: Mapped[str | None] = mapped_column(String(5), nullable=True, default="07:30")
    """Утренняя сводка «ЧЧ:ММ» по Москве; None — выключена."""
    digest_sent_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    notify_changes: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_certificates: Mapped[bool] = mapped_column(Boolean, default=True)
    exam_reminder_days: Mapped[int | None] = mapped_column(Integer, nullable=True, default=3)
    """За сколько дней напомнить о зачёте/экзамене; None — не напоминать."""
    reminder_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Напоминание о паре внутри событий календаря (VALARM); None — без него."""


class TelegramLinkCode(Base):
    __tablename__ = "telegram_link_codes"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class BotCursor(Base):
    """До какой записи журнала бот уже дочитал (например, `lesson_changes`)."""

    __tablename__ = "bot_cursors"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class BotNotice(Base):
    """Что уже сказано студенту — чтобы напоминание не повторилось после перезапуска."""

    __tablename__ = "bot_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    key: Mapped[str] = mapped_column(String(100))
    sent_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("student_id", "key", name="uq_bot_notices_student_key"),)
