"""Учётные записи и серверные сессии (решение docs/decisions/0001-architecture.md, «Безопасность»).

Пароль хранится только хэшем argon2. В cookie лежит случайный токен; в БД — его SHA-256,
так что утечка таблицы `auth_sessions` не даёт войти чужой сессией.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Role:
    STUDENT = "student"
    STAFF = "staff"
    TEACHER = "teacher"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=Role.STUDENT)
    interface_language: Mapped[str] = mapped_column(String(2), default="ru", server_default="ru")
    student_id: Mapped[int | None] = mapped_column(
        ForeignKey("students.id"), nullable=True, unique=True
    )
    """Для роли student — чья это учётка. Все запросы к данным студента фильтруются по нему."""
    teacher_id: Mapped[int | None] = mapped_column(
        ForeignKey("teachers.id"), nullable=True, unique=True
    )
    """Для роли teacher — какой преподаватель из `teachers`. Ведомости фильтруются по нему."""
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    """SHA-256 от токена из cookie (hex)."""
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    """Проверяется на каждом POST из форм кабинета (поле `csrf` или заголовок X-CSRF-Token)."""
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
