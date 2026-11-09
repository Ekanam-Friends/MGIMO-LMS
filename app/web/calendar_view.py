"""Календарь студента из трёх слоёв (решение docs/decisions/0006-calendar-layers.md).

1. «Обязательные пары» — занятия группы из РУЗ, которые не относятся к выбору.
2. «Элективы и подгруппы» — занятия по выбору. В РУЗ они не помечены отдельно, поэтому
   определяются по самим данным: у занятия есть подгруппа (языки) или в то же время у группы
   идёт другая дисциплина без подгруппы (параллельные курсы по выбору). Показываются только
   те, что студент отметил в разделе «Элективные дисциплины».
3. «Внеучебные активности» — вымышленные мероприятия; записанные показаны ярко, остальные
   бледно, клик по бледному — запись.

Группа и выбор берутся только по студенту из сессии.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Activity, ActivitySignup, ElectiveChoice, Lesson, LessonGroup, Student, Teacher, lesson_teachers
from app.models.calendar import ChoiceKind
from app.models.schedule import LessonStatus
from app.web.schedule_view import LessonView

LAYERS = [
    ("core", "Обязательные пары"),
    ("elective", "Элективы и подгруппы"),
    ("activity", "Внеучебные активности"),
]
GRID_FROM, GRID_TO = 8, 21  # часы сетки


def subgroup_parts(subgroup: str | None) -> list[str]:
    """Объединённые занятия в РУЗ приходят как «06/1-англ/испан (3МЖ-02); 06-англ/испан (3МЖ-02)»."""
    return [p.strip() for p in (subgroup or "").split(";") if p.strip()]


@dataclass
class ElectiveCatalog:
    """Что в расписании группы относится к выбору."""

    subgroups: dict[str, list[str]] = field(default_factory=dict)
    """Подгруппа → её дисциплины."""
    families: list[list[str]] = field(default_factory=list)
    """Наборы дисциплин, которые идут параллельно друг другу (выбрать одну или несколько)."""

    @property
    def disciplines(self) -> set[str]:
        return {d for fam in self.families for d in fam}

    def is_elective(self, discipline: str, subgroup: str | None) -> bool:
        return bool(subgroup_parts(subgroup)) or discipline in self.disciplines


async def elective_catalog(db: AsyncSession, group_id: int | None) -> ElectiveCatalog:
    if group_id is None:
        return ElectiveCatalog()
    rows = (await db.execute(
        select(Lesson.date, Lesson.begins_at, Lesson.discipline, Lesson.subgroup)
        .join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
        .where(LessonGroup.group_id == group_id)
        .distinct()
    )).all()
    subgroups: dict[str, set[str]] = defaultdict(set)
    slots: dict[tuple[date, time], set[str]] = defaultdict(set)
    for d, t, discipline, subgroup in rows:
        parts = subgroup_parts(subgroup)
        if len(parts) == 1:  # объединения с чужими группами — не отдельный вариант выбора
            subgroups[parts[0]].add(discipline)
        elif not parts:
            slots[(d, t)].add(discipline)

    # Дисциплины, которые хоть раз шли параллельно, связываем в «семьи» (компоненты связности).
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for names in slots.values():
        if len(names) > 1:
            first, *rest = sorted(names)
            for n in rest:
                parent[find(n)] = find(first)
    fams: dict[str, list[str]] = defaultdict(list)
    for n in list(parent):
        fams[find(n)].append(n)
    return ElectiveCatalog(
        subgroups={k: sorted(v) for k, v in sorted(subgroups.items())},
        families=sorted(sorted(f) for f in fams.values()),
    )


async def get_choices(db: AsyncSession, student_id: int) -> tuple[set[str], set[str]]:
    rows = (await db.execute(
        select(ElectiveChoice.kind, ElectiveChoice.value).where(ElectiveChoice.student_id == student_id)
    )).all()
    return ({v for k, v in rows if k == ChoiceKind.SUBGROUP}, {v for k, v in rows if k == ChoiceKind.DISCIPLINE})


async def set_choices(db: AsyncSession, student_id: int, catalog: ElectiveCatalog,
                      subgroups: list[str], disciplines: list[str]) -> None:
    """Перезаписывает выбор студента. Принимаются только варианты из расписания его группы."""
    rows = await db.scalars(select(ElectiveChoice).where(ElectiveChoice.student_id == student_id))
    for r in rows.all():
        await db.delete(r)
    for s in sorted(set(subgroups) & set(catalog.subgroups)):
        db.add(ElectiveChoice(student_id=student_id, kind=ChoiceKind.SUBGROUP, value=s))
    for d in sorted(set(disciplines) & catalog.disciplines):
        db.add(ElectiveChoice(student_id=student_id, kind=ChoiceKind.DISCIPLINE, value=d))


@dataclass
class CalEvent:
    layer: str  # core | elective | activity
    title: str
    begins_at: time
    ends_at: time
    kind: str = ""
    kind_class: str = "seminar"
    place: str = ""
    auditorium: str = ""
    building: str = ""
    teachers: list[str] = field(default_factory=list)
    subgroup: str = ""
    cancelled: bool = False
    activity_id: int | None = None
    signed: bool = False
    organizer: str = ""
    lane: int = 0
    lanes: int = 1

    @property
    def top(self) -> int:
        """Минуты от начала сетки."""
        return max(0, (self.begins_at.hour - GRID_FROM) * 60 + self.begins_at.minute)

    @property
    def length(self) -> int:
        end = min(self.ends_at.hour * 60 + self.ends_at.minute, GRID_TO * 60)
        return max(20, end - GRID_FROM * 60 - self.top)


def _assign_lanes(events: list[CalEvent]) -> None:
    """Пересекающиеся события делят ширину колонки дня."""
    events.sort(key=lambda e: (e.begins_at, e.ends_at))
    cluster: list[CalEvent] = []
    cluster_end = time.min
    lane_ends: list[time] = []

    def close() -> None:
        for e in cluster:
            e.lanes = len(lane_ends)

    for e in events:
        if cluster and e.begins_at >= cluster_end:
            close()
            cluster, lane_ends = [], []
        for i, end in enumerate(lane_ends):
            if end <= e.begins_at:
                e.lane, lane_ends[i] = i, e.ends_at
                break
        else:
            e.lane = len(lane_ends)
            lane_ends.append(e.ends_at)
        cluster.append(e)
        cluster_end = max(cluster_end, e.ends_at)
    if cluster:
        close()


async def events_for(db: AsyncSession, student: Student, start: date, end: date) -> dict[date, list[CalEvent]]:
    """События студента за [start, end] по дням: свои пары, выбранные элективы, активности."""
    days: dict[date, list[CalEvent]] = defaultdict(list)
    group_id = student.study_group_id
    if group_id is not None:
        catalog = await elective_catalog(db, group_id)
        my_subgroups, my_disciplines = await get_choices(db, student.id)
        rows = (await db.execute(
            select(Lesson)
            .join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
            .where(LessonGroup.group_id == group_id, Lesson.date >= start, Lesson.date <= end)
            .order_by(Lesson.date, Lesson.begins_at, Lesson.discipline)
        )).scalars().all()
        teachers: dict[int, list[str]] = defaultdict(list)
        if rows:
            for lesson_id, name in (await db.execute(
                select(lesson_teachers.c.lesson_id, Teacher.full_name)
                .join(Teacher, Teacher.id == lesson_teachers.c.teacher_id)
                .where(lesson_teachers.c.lesson_id.in_([r.id for r in rows]))
            )).all():
                teachers[lesson_id].append(name)
        for r in rows:
            elective = catalog.is_elective(r.discipline, r.subgroup)
            if elective and not (set(subgroup_parts(r.subgroup)) & my_subgroups or r.discipline in my_disciplines):
                continue
            kind = r.kind_of_work or "Занятие"
            lv = LessonView(id=r.id, discipline=r.discipline, kind=kind, begins_at=r.begins_at, ends_at=r.ends_at,
                            auditorium="", building="", subgroup="", teachers=[], cancelled=False)
            days[r.date].append(CalEvent(
                layer="elective" if elective else "core", title=r.discipline, kind=kind, kind_class=lv.kind_class,
                begins_at=r.begins_at, ends_at=r.ends_at,
                place=", ".join(p for p in (r.auditorium, r.building) if p),
                auditorium=r.auditorium or "", building=r.building or "",
                teachers=sorted(teachers.get(r.id, [])), subgroup=r.subgroup or "",
                cancelled=r.status == LessonStatus.CANCELLED,
            ))

    acts = (await db.scalars(
        select(Activity).where(Activity.starts_at >= datetime.combine(start, time.min),
                               Activity.starts_at < datetime.combine(end + timedelta(days=1), time.min))
    )).all()
    signed = set((await db.scalars(
        select(ActivitySignup.activity_id).where(ActivitySignup.student_id == student.id)
    )).all())
    for a in acts:
        days[a.starts_at.date()].append(activity_event(a, a.id in signed))

    for evs in days.values():
        _assign_lanes(evs)
    return dict(days)


def activity_event(a: Activity, signed: bool) -> CalEvent:
    return CalEvent(layer="activity", title=a.title, kind="Активность", kind_class="activity",
                    begins_at=a.starts_at.time(), ends_at=a.ends_at.time(), place=a.place, auditorium=a.place,
                    organizer=a.organizer, activity_id=a.id, signed=signed)


async def set_signup(db: AsyncSession, student_id: int, activity_id: int, on: bool) -> Activity | None:
    """Запись/отмена записи. None — мероприятия нет (→ 404)."""
    activity = await db.get(Activity, activity_id)
    if activity is None:
        return None
    row = await db.get(ActivitySignup, (student_id, activity_id))
    if on and row is None:
        db.add(ActivitySignup(student_id=student_id, activity_id=activity_id, created_at=datetime.utcnow()))
    elif not on and row is not None:
        await db.delete(row)
    return activity
