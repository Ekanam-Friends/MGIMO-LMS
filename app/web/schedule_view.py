"""Расписание для экранов кабинета: чтение из таблиц импорта РУЗ.

Группа берётся только из записи студента, найденной по id из сессии, — студент не может
подставить чужую группу параметром запроса.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lesson, LessonGroup, Teacher, lesson_teachers
from app.models.schedule import LessonStatus

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]


@dataclass
class LessonView:
    id: int
    discipline: str
    kind: str
    begins_at: time
    ends_at: time
    auditorium: str
    building: str
    subgroup: str
    teachers: list[str]
    cancelled: bool

    @property
    def kind_class(self) -> str:
        k = self.kind.lower()
        if k.startswith("лекц"):
            return "lecture"
        if k.startswith(("экзам", "зачет", "зачёт", "контрол")):
            return "exam"
        if k.startswith("консульт"):
            return "consult"
        return "seminar"


@dataclass
class Slot:
    """Одна пара на экране. Параллельные занятия по подгруппам (языки) сворачиваются в один слот."""

    begins_at: time
    ends_at: time
    lessons: list[LessonView] = field(default_factory=list)

    @property
    def grouped(self) -> bool:
        return len(self.lessons) > 1


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def lessons_for(
    session: AsyncSession, group_id: int | None, start: date, end: date
) -> dict[date, list[Slot]]:
    """Занятия группы за [start, end], разложенные по дням и парам."""
    if group_id is None:
        return {}
    rows = (await session.execute(
        select(Lesson)
        .join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
        .where(LessonGroup.group_id == group_id, Lesson.date >= start, Lesson.date <= end)
        .order_by(Lesson.date, Lesson.begins_at, Lesson.discipline)
    )).scalars().all()
    if not rows:
        return {}
    teacher_rows = (await session.execute(
        select(lesson_teachers.c.lesson_id, Teacher.full_name)
        .join(Teacher, Teacher.id == lesson_teachers.c.teacher_id)
        .where(lesson_teachers.c.lesson_id.in_([r.id for r in rows]))
    )).all()
    teachers: dict[int, list[str]] = defaultdict(list)
    for lesson_id, name in teacher_rows:
        teachers[lesson_id].append(name)

    days: dict[date, list[Slot]] = defaultdict(list)
    for r in rows:
        view = LessonView(
            id=r.id, discipline=r.discipline, kind=r.kind_of_work or "Занятие",
            begins_at=r.begins_at, ends_at=r.ends_at, auditorium=r.auditorium or "",
            building=r.building or "", subgroup=r.subgroup or "",
            teachers=sorted(teachers.get(r.id, [])), cancelled=r.status == LessonStatus.CANCELLED,
        )
        slots = days[r.date]
        if slots and slots[-1].begins_at == r.begins_at and slots[-1].ends_at == r.ends_at:
            slots[-1].lessons.append(view)
        else:
            slots.append(Slot(begins_at=r.begins_at, ends_at=r.ends_at, lessons=[view]))
    return dict(days)


async def days_with_lessons(session: AsyncSession, group_id: int | None, start: date, end: date) -> set[date]:
    if group_id is None:
        return set()
    rows = await session.scalars(
        select(Lesson.date).distinct()
        .join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
        .where(LessonGroup.group_id == group_id, Lesson.date >= start, Lesson.date <= end,
               Lesson.status == LessonStatus.ACTIVE)
    )
    return set(rows.all())
