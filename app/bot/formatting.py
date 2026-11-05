"""Расписание текстом для чата (разметка HTML).

Формат — как в ikanam_schedule_bot: читают на ходу, с телефона, за минуту до пары. Время
первым и жирным, под предметом — аудитория, потом преподаватель. Данные — те же, что
у экрана «Расписание» кабинета (`app.web.schedule_view`).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from html import escape

from app.web.schedule_view import LessonView, Slot

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS_GEN = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря")

TELEGRAM_LIMIT = 4096


def format_date(day: date) -> str:
    """«четверг, 5 ноября» — без года."""
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTHS_GEN[day.month - 1]}"


def short_kind(raw: str) -> str:
    k = raw.strip().lower()
    if k.startswith("лекц"):
        return "лекция"
    if k.startswith("семин"):
        return "семинар"
    if k.startswith("практ"):
        return "практика"
    return k or "занятие"


def _place(lesson: LessonView) -> str:
    if not lesson.auditorium:
        return "аудитория уточняется"
    where = escape(lesson.auditorium)
    if lesson.building and lesson.building not in lesson.auditorium:
        where += f", {escape(lesson.building)}"
    return where


def format_lesson(lesson: LessonView, *, with_time: str | None = None) -> str:
    title = f"{escape(lesson.discipline)} ({escape(short_kind(lesson.kind))})"
    if lesson.subgroup:
        title += f" · {escape(lesson.subgroup)}"
    if lesson.cancelled:
        title = f"<s>{title}</s> — отменено"
    lines = [f"{with_time}  {title}" if with_time else title, "    " + _place(lesson)]
    if lesson.teachers:
        lines.append("    " + escape(", ".join(lesson.teachers)))
    return "\n".join(lines)


def format_slot(slot: Slot) -> str:
    time = f"<b>{slot.begins_at:%H:%M}–{slot.ends_at:%H:%M}</b>"
    if not slot.grouped:
        return format_lesson(slot.lessons[0], with_time=time)
    # Параллельные занятия подгрупп (языки) — под одним временем.
    parts = [f"{time}  по подгруппам:"]
    parts += ["  • " + format_lesson(lesson).replace("\n", "\n  ") for lesson in slot.lessons]
    return "\n".join(parts)


def format_day(day: date, slots: list[Slot], *, title: str | None = None) -> str:
    header = f"<b>{escape(title or format_date(day).capitalize())}</b>"
    if not slots:
        return f"{header}\nЗанятий нет."
    return header + "\n\n" + "\n\n".join(format_slot(s) for s in slots)


def format_week(since: date, days: dict[date, list[Slot]], *, length: int = 7) -> str:
    """Неделя одним сообщением; дни без занятий — одной строкой."""
    parts: list[str] = []
    for offset in range(length):
        current = since + timedelta(days=offset)
        slots = days.get(current, [])
        if slots:
            parts.append(format_day(current, slots))
        else:
            parts.append(f"<b>{escape(format_date(current).capitalize())}</b> — занятий нет")
    return clip("\n\n".join(parts))


def clip(text: str, limit: int = TELEGRAM_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit - 60)
    return text[: cut if cut > 0 else limit - 60] + "\n\n… не поместилось — полное расписание в кабинете."


def is_exam(lesson: LessonView) -> bool:
    return lesson.kind_class == "exam" and not lesson.cancelled


def format_exams(items: Iterable[tuple[date, Slot, LessonView]], *, today: date) -> str:
    lines: list[str] = []
    for day, slot, lesson in items:
        left = (day - today).days
        when = "сегодня" if left == 0 else "завтра" if left == 1 else f"через {left} дн."
        lines.append(
            f"<b>{day:%d.%m}</b> {slot.begins_at:%H:%M} — {escape(lesson.discipline)} "
            f"({escape(short_kind(lesson.kind))}), {_place(lesson)} · <i>{when}</i>"
        )
    if not lines:
        return "<b>Контрольные точки</b>\n\nВ ближайшие 60 дней зачётов, экзаменов и контрольных в расписании нет."
    return clip("<b>Контрольные точки</b> — зачёты, экзамены и контрольные из расписания\n\n" + "\n".join(lines))
