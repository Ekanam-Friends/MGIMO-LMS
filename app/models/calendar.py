"""Слои календаря студента (решение docs/decisions/0006-calendar-layers.md).

* `ElectiveChoice` — что студент отметил «своим» в расписании группы из РУЗ: языковую
  подгруппу (`kind="subgroup"`, значение — строка подгруппы из РУЗ) или дисциплину по выбору,
  которая идёт параллельно с другими (`kind="discipline"`). Невыбранные занятия по выбору
  в календаре не показываются.
* `Activity` / `ActivitySignup` — внеучебные мероприятия (вымышленные, из seed) и запись на них.
  Запись — только за студента из сессии.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ChoiceKind:
    SUBGROUP = "subgroup"
    DISCIPLINE = "discipline"


class ElectiveChoice(Base):
    __tablename__ = "elective_choices"

    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), primary_key=True)


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    place: Mapped[str] = mapped_column(String(300), default="")
    organizer: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (UniqueConstraint("title", "starts_at", name="uq_activities_title_start"),)


class ActivitySignup(Base):
    __tablename__ = "activity_signups"

    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)
    activity_id: Mapped[int] = mapped_column(ForeignKey("activities.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
