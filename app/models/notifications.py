"""Объявления и отметки о прочтении для колокольчика в шапке (решение docs/decisions/0009-notifications.md).

В колокольчик попадает не только эта таблица: статусы справок, новые баллы и ДЗ читаются из
уже существующих журналов (`certificate_request_events`, `grade_events`), см. app/notifications/service.py.
Здесь — только то, что люди пишут руками, и что прочитано.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Source:
    DEAN = "dean"
    TEACHER = "teacher"
    UNIVERSITY = "university"

    TITLES = {DEAN: "Деканат", TEACHER: "Преподаватель", UNIVERSITY: "Вуз"}


class Audience:
    ALL = "all"
    FACULTY = "faculty"
    GROUP = "group"


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    author_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(String(20), default=Audience.ALL)
    faculty_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Для audience=faculty: название как в `students.faculty_name`."""
    group_id: Mapped[int | None] = mapped_column(ForeignKey("study_groups.id"), nullable=True)
    """Для audience=group: группа, которой адресовано (преподаватель — только своим)."""
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    group = relationship("StudyGroup", lazy="joined")


class NotificationRead(Base):
    """Что пользователь уже открыл. `key` — «ann:12», «cert:7», «grade:31»."""

    __tablename__ = "notification_reads"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    read_at: Mapped[datetime] = mapped_column(DateTime)
