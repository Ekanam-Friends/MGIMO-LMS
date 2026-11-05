"""Расписание, импортированное из РУЗ МГИМО.

Первичные ключи факультетов, групп и занятий — это их oid в РУЗ: так повторный импорт
узнаёт уже известные записи без отдельной таблицы соответствий.

Реальных ФИО и email преподавателей здесь нет и быть не должно: в `teachers` хранится
только oid из РУЗ и сгенерированное вымышленное ФИО (см. app/schedule/fake_names.py).
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LessonStatus:
    ACTIVE = "active"
    CANCELLED = "cancelled"


class ChangeKind:
    ADDED = "added"
    CHANGED = "changed"
    MOVED = "moved"
    """Сменились дата или время. Либо та же запись РУЗ, либо пара «отмена + новое занятие»."""
    CANCELLED = "cancelled"
    RESTORED = "restored"


class ImportStatus:
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    """Часть групп не загрузилась; по ним отмены не фиксируются."""
    FAILED = "failed"


class Faculty(Base):
    __tablename__ = "faculties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    abbr: Mapped[str] = mapped_column(String(200), default="")
    name: Mapped[str] = mapped_column(String(500))


class StudyGroup(Base):
    __tablename__ = "study_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(100), index=True)
    """Имя по году набора, как в РУЗ: «26МО-01»."""
    number: Mapped[str] = mapped_column(String(100), index=True)
    """Имя по курсу, как в поиске РУЗ: «1МО-01»."""
    faculty_id: Mapped[int | None] = mapped_column(ForeignKey("faculties.id"), nullable=True)
    course: Mapped[int | None] = mapped_column(Integer, nullable=True)
    form_of_education: Mapped[str] = mapped_column(String(100), default="")
    speciality: Mapped[str] = mapped_column(String(500), default="")
    specialization: Mapped[str] = mapped_column(String(500), default="")


class Teacher(Base):
    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ruz_oid: Mapped[int] = mapped_column(Integer, unique=True)
    full_name: Mapped[str] = mapped_column(String(200), unique=True)
    """Вымышленное ФИО. Реального здесь нет."""


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default=ImportStatus.RUNNING)
    groups_total: Mapped[int] = mapped_column(Integer, default=0)
    groups_ok: Mapped[int] = mapped_column(Integer, default=0)
    groups_failed: Mapped[int] = mapped_column(Integer, default=0)
    lessons_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Lesson(Base):
    __tablename__ = "lessons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    """lessonOid из РУЗ."""
    date: Mapped[date] = mapped_column(Date, index=True)
    begins_at: Mapped[time] = mapped_column(Time)
    ends_at: Mapped[time] = mapped_column(Time)
    lesson_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discipline: Mapped[str] = mapped_column(String(500))
    kind_of_work: Mapped[str] = mapped_column(String(200), default="")
    auditorium: Mapped[str | None] = mapped_column(String(200), nullable=True)
    building: Mapped[str | None] = mapped_column(String(200), nullable=True)
    stream: Mapped[str | None] = mapped_column(Text, nullable=True)
    subgroup: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default=LessonStatus.ACTIVE)
    moved_to_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"), nullable=True)
    """Для отменённого занятия, которое перенесли новой записью РУЗ, — куда перенесли."""

    ruz_modified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime)
    last_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_runs.id"), nullable=True, index=True
    )


class LessonGroup(Base):
    """Какие группы видят занятие. Одна лекция потока — у десятков групп."""

    __tablename__ = "lesson_groups"

    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("study_groups.id"), primary_key=True)
    last_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_runs.id"), nullable=True
    )

    __table_args__ = (Index("ix_lesson_groups_group", "group_id", "lesson_id"),)


lesson_teachers = Table(
    "lesson_teachers",
    Base.metadata,
    Column("lesson_id", ForeignKey("lessons.id"), primary_key=True),
    Column("teacher_id", ForeignKey("teachers.id"), primary_key=True),
)


class LessonChange(Base):
    """Журнал изменений расписания: из него подсветка в кабинете и уведомления в бот."""

    __tablename__ = "lesson_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("import_runs.id"), index=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"), index=True)
    previous_lesson_id: Mapped[int | None] = mapped_column(
        ForeignKey("lessons.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime)
