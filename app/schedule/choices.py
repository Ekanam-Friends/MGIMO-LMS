"""Выбор подгрупп и элективов студента для календаря и ассистента."""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, time
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import ElectiveChoice, Lesson, LessonGroup
from app.models.calendar import ChoiceKind

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


def selected_lesson(discipline, subgroup, catalog, choices):
    subgroups, disciplines = choices
    return (not catalog.is_elective(discipline, subgroup)
            or bool(set(subgroup_parts(subgroup)) & subgroups)
            or discipline in disciplines)
