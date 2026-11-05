"""Перевод сырых ответов РУЗ в наши структуры.

Это единственное место, где код видит сырой ответ, поэтому здесь работает белый список:
из занятия берутся только перечисленные поля. Всё про преподавателя, кроме его oid
(`lecturer`, `lecturerEmail`, `listOfLecturers[].lecturer`, `lecturer_rank`,
`lecturer_title`, `author`), сюда не попадает и дальше не идёт. Свободный текст
(`note`, `replaces`, `detailInfo`) тоже отбрасывается: в нём может оказаться ФИО.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any


@dataclass(frozen=True, slots=True)
class FacultyData:
    id: int
    abbr: str
    name: str


@dataclass(frozen=True, slots=True)
class GroupData:
    id: int
    name: str
    number: str
    faculty_id: int | None
    course: int | None
    form_of_education: str
    speciality: str
    specialization: str
    has_schedule: bool


@dataclass(frozen=True, slots=True)
class LessonData:
    id: int
    date: date
    begins_at: time
    ends_at: time
    lesson_number: int | None
    discipline: str
    kind_of_work: str
    auditorium: str | None
    building: str | None
    stream: str | None
    subgroup: str | None
    ruz_modified_at: datetime | None
    lecturer_oids: tuple[int, ...]


def _str(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _opt_str(value: Any) -> str | None:
    text = _str(value)
    return text or None


def _opt_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _parse_time(value: Any) -> time:
    return time.fromisoformat(_str(value)[:5])


def _parse_ruz_datetime(value: Any) -> datetime | None:
    # РУЗ отдаёт даты вида «2026-06-03T11:38:16Z00:00» — берём только дату и время.
    text = _str(value)
    if len(text) < 19:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def real_surnames(raw_lecturers: list[dict[str, Any]]) -> frozenset[str]:
    """Фамилии реальных преподавателей — только для фильтра вымышленных имён.

    Результат живёт в памяти одного импорта и никуда не сохраняется.
    """
    result: set[str] = set()
    for raw in raw_lecturers:
        for key in ("fio", "shortFIO"):
            parts = _str(raw.get(key)).split()
            if parts:
                result.add(parts[0].strip(".,"))
    return frozenset(s for s in result if s)


def faculty_from_raw(raw: dict[str, Any]) -> FacultyData:
    return FacultyData(id=int(raw["facultyOid"]), abbr=_str(raw.get("abbr")), name=_str(raw.get("name")))


def group_from_raw(raw: dict[str, Any]) -> GroupData:
    faculty_id = _opt_int(raw.get("facultyOid"))
    return GroupData(
        id=int(raw["groupOid"]),
        name=_str(raw.get("name")),
        number=_str(raw.get("number")) or _str(raw.get("name")),
        faculty_id=faculty_id or None,
        course=_opt_int(raw.get("course")),
        form_of_education=_str(raw.get("formOfEducation")),
        speciality=_str(raw.get("speciality")),
        specialization=_str(raw.get("groupSpecialization_name")),
        has_schedule=bool(_opt_int(raw.get("is_schedule")) or 0),
    )


def lesson_from_raw(raw: dict[str, Any]) -> LessonData:
    oids: list[int] = []
    for lecturer in raw.get("listOfLecturers") or []:
        oid = _opt_int(lecturer.get("lecturerOid"))
        if oid and oid not in oids:
            oids.append(oid)
    if not oids:
        oid = _opt_int(raw.get("lecturerOid"))
        if oid:
            oids.append(oid)

    return LessonData(
        id=int(raw["lessonOid"]),
        date=date.fromisoformat(_str(raw["date"])[:10]),
        begins_at=_parse_time(raw["beginLesson"]),
        ends_at=_parse_time(raw["endLesson"]),
        lesson_number=_opt_int(raw.get("lessonNumberStart")),
        discipline=_str(raw.get("discipline")),
        kind_of_work=_str(raw.get("kindOfWork")),
        auditorium=_opt_str(raw.get("auditorium")),
        building=_opt_str(raw.get("building")),
        stream=_opt_str(raw.get("stream")),
        subgroup=_opt_str(raw.get("subGroup")),
        ruz_modified_at=_parse_ruz_datetime(raw.get("modifieddate")),
        lecturer_oids=tuple(oids),
    )
