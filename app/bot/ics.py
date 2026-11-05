"""Расписание в формате iCalendar: файл `.ics` в чат и лента подписки `/feed/<токен>.ics`.

Перенесено из ikanam_schedule_bot (`app/calendar/ics.py`) с теми же решениями:

* время пишется в UTC (`...Z`), без блока VTIMEZONE: у Москвы нет летнего времени,
  а Google, Apple и Outlook одинаково понимают только UTC;
* отменённое занятие остаётся в ленте со `STATUS:CANCELLED` — если просто перестать его
  отдавать, многие клиенты оставят событие висеть;
* `SEQUENCE` растёт с каждой записью в журнале `lesson_changes`: без этого календарь
  не примет обновлённое событие.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Alarm, Calendar, Event, vDuration
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ImportRun, LessonChange
from app.models.schedule import ImportStatus
from app.web.schedule_view import LessonView, lessons_for

MOSCOW = ZoneInfo("Europe/Moscow")
PRODID = "-//Ekanam Friends//MGIMO-LMS concept//RU"
CALENDAR_NAME = "Расписание МГИМО (концепт)"

FEED_BACK = timedelta(days=7)
FEED_AHEAD = timedelta(days=60)
FILE_AHEAD = timedelta(days=21)


def short_name(full_name: str) -> str:
    """«Иванов Пётр Сергеевич» → «Иванов П. С.»."""
    parts = full_name.split()
    if len(parts) < 2:
        return full_name
    return parts[0] + " " + " ".join(f"{p[0]}." for p in parts[1:] if p and p[0].isalpha())


def _utc(day: date, t) -> datetime:
    return datetime.combine(day, t, tzinfo=MOSCOW).astimezone(timezone.utc)


def _event(day: date, lesson: LessonView, *, stamp: datetime, sequence: int, reminder_minutes: int | None) -> Event:
    event = Event()
    event.add("uid", f"lesson-{lesson.id}@mgimo-lms.concept")
    event.add("dtstamp", stamp)
    event.add("last-modified", stamp)
    event.add("dtstart", _utc(day, lesson.begins_at))
    event.add("dtend", _utc(day, lesson.ends_at))
    title = lesson.discipline
    if lesson.teachers:
        title += " · " + short_name(lesson.teachers[0])
    event.add("summary", f"Отменено: {title}" if lesson.cancelled else title)
    location = lesson.auditorium
    if lesson.building and lesson.building not in location:
        location = f"{location}, {lesson.building}" if location else lesson.building
    if location:
        event.add("location", location)
    description = [lesson.kind]
    if lesson.subgroup:
        description.append(f"Подгруппа: {lesson.subgroup}")
    description += lesson.teachers
    if lesson.cancelled:
        description.append("Занятие отменено")
    event.add("description", "\n".join(description))
    event.add("sequence", sequence)
    event.add("status", "CANCELLED" if lesson.cancelled else "CONFIRMED")
    event.add("transp", "OPAQUE")
    if reminder_minutes and not lesson.cancelled:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", lesson.discipline)
        alarm.add("trigger", timedelta(minutes=-reminder_minutes))
        event.add_component(alarm)
    return event


async def build_calendar(
    session: AsyncSession,
    group_id: int | None,
    *,
    start: date,
    end: date,
    for_subscription: bool,
    reminder_minutes: int | None = None,
) -> bytes:
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", CALENDAR_NAME)
    cal.add("x-wr-timezone", "Europe/Moscow")
    if for_subscription:
        # vDuration явно: иначе библиотека пишет «4:00:00», и клиенты молча игнорируют поле.
        interval = vDuration(timedelta(hours=4))
        cal.add("refresh-interval", interval, parameters={"VALUE": "DURATION"})
        cal.add("x-published-ttl", interval)

    days = await lessons_for(session, group_id, start, end)
    ids = [lesson.id for slots in days.values() for slot in slots for lesson in slot.lessons]
    sequences: Counter[int] = Counter()
    if ids:
        rows = await session.execute(
            select(LessonChange.lesson_id, func.count()).where(LessonChange.lesson_id.in_(ids)).group_by(LessonChange.lesson_id)
        )
        sequences.update({lesson_id: n for lesson_id, n in rows.all()})
    # Штамп — время последнего импорта, а не сборки ответа: иначе лента «меняется» при каждом
    # запросе, и клиенты считают изменёнными все события.
    last = await session.scalar(
        select(func.max(ImportRun.finished_at)).where(ImportRun.status.in_([ImportStatus.OK, ImportStatus.PARTIAL]))
    )
    stamp = (last.replace(tzinfo=timezone.utc) if last else datetime(2026, 1, 1, tzinfo=timezone.utc))

    for day in sorted(days):
        for slot in days[day]:
            for lesson in slot.lessons:
                cal.add_component(_event(day, lesson, stamp=stamp, sequence=sequences[lesson.id],
                                         reminder_minutes=reminder_minutes))
    return cal.to_ical()
