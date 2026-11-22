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

from app.models import Lesson, LessonGroup, StudyGroup, Teacher, lesson_teachers
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
    groups: list[str] = field(default_factory=list)
    """Для расписания преподавателя: какие группы на занятии."""

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
    return await _arrange(session, list(rows))


async def teacher_lessons_for(
    session: AsyncSession, teacher_id: int | None, start: date, end: date
) -> dict[date, list[Slot]]:
    """Занятия преподавателя за [start, end] с группами на каждом. id — только из сессии."""
    if teacher_id is None:
        return {}
    rows = (await session.execute(
        select(Lesson)
        .join(lesson_teachers, lesson_teachers.c.lesson_id == Lesson.id)
        .where(lesson_teachers.c.teacher_id == teacher_id, Lesson.date >= start, Lesson.date <= end)
        .order_by(Lesson.date, Lesson.begins_at, Lesson.discipline)
    )).scalars().all()
    if not rows:
        return {}
    group_rows = (await session.execute(
        select(LessonGroup.lesson_id, StudyGroup.number)
        .join(StudyGroup, StudyGroup.id == LessonGroup.group_id)
        .where(LessonGroup.lesson_id.in_([r.id for r in rows]))
    )).all()
    groups: dict[int, list[str]] = defaultdict(list)
    for lesson_id, number in group_rows:
        groups[lesson_id].append(number)
    return await _arrange(session, list(rows), groups)


async def _arrange(
    session: AsyncSession, rows: list[Lesson], groups: dict[int, list[str]] | None = None
) -> dict[date, list[Slot]]:
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
            groups=sorted((groups or {}).get(r.id, [])),
        )
        slots = days[r.date]
        if slots and slots[-1].begins_at == r.begins_at and slots[-1].ends_at == r.ends_at:
            slots[-1].lessons.append(view)
        else:
            slots.append(Slot(begins_at=r.begins_at, ends_at=r.ends_at, lessons=[view]))
    return dict(days)


async def teacher_next_day(session: AsyncSession, teacher_id: int | None, after: date, within: int = 30) -> date | None:
    """Ближайший день после `after` (не дальше `within` дней), когда у преподавателя есть неотменённые занятия."""
    if teacher_id is None:
        return None
    return await session.scalar(
        select(Lesson.date)
        .join(lesson_teachers, lesson_teachers.c.lesson_id == Lesson.id)
        .where(lesson_teachers.c.teacher_id == teacher_id, Lesson.date > after,
               Lesson.date <= after + timedelta(days=within), Lesson.status == LessonStatus.ACTIVE)
        .order_by(Lesson.date).limit(1)
    )


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
