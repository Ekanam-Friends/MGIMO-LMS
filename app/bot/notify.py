"""Что бот сам пишет студенту: изменения расписания, статусы справок, напоминания о зачётах.

Источники — готовые журналы кабинета, бот ничего не сравнивает сам:

* `lesson_changes` — пишет импорт РУЗ (app/schedule/importer.py). Бот читает записи с id
  больше курсора `bot_cursors["lesson_changes"]` и раскладывает по группам занятия;
* `certificate_request_events` — пишет сверка с Pyrus. Бот берёт строки с пустым
  `notified_at` и проставляет его, даже если студент бота не подключал: иначе после
  подключения на него вывалилась бы вся история заявок;
* напоминание о контрольной точке — по расписанию; что уже сказано, лежит в `bot_notices`.

Функции здесь не знают про Telegram: возвращают тексты по student_id, отправляет scheduler.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from html import escape

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.formatting import format_date, short_kind
from app.bot.link import utcnow
from app.models import (
    BotCursor,
    BotNotice,
    CertificateRequest,
    CertificateRequestEvent,
    Lesson,
    LessonChange,
    LessonGroup,
    Student,
    TelegramLink,
)
from app.models.certificates import CertificateKind, EventKind, PaperStatus, RequestStatus
from app.models.schedule import ChangeKind

CHANGES_CURSOR = "lesson_changes"
CHANGE_HORIZON = timedelta(days=21)
"""О переносах дальше трёх недель не пишем: это перекройка семестра, а не новость дня."""

FIELD_TITLES = {"auditorium": "аудитория", "building": "корпус", "discipline": "дисциплина",
                "kind_of_work": "вид занятия", "teachers": "преподаватель"}


def _d(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        return format_date(date.fromisoformat(raw))
    except ValueError:
        return raw


def _t(raw: str | None) -> str:
    return (raw or "")[:5]


def _snap_when(s: dict) -> str:
    return f"{_d(s.get('date'))}, {_t(s.get('begins_at'))}"


def describe_change(change: LessonChange, lesson: Lesson) -> str:
    title = f"<b>{escape(lesson.discipline)}</b> ({escape(short_kind(lesson.kind_of_work or ''))})"
    d = change.details or {}
    if change.kind == ChangeKind.CANCELLED:
        return f"❌ Отменено: {title}, {_snap_when(d.get('lesson', {}))}"
    if change.kind == ChangeKind.ADDED:
        s = d.get("lesson", {})
        where = f", ауд. {escape(s['auditorium'])}" if s.get("auditorium") else ""
        return f"➕ Новое занятие: {title}, {_snap_when(s)}{where}"
    if change.kind == ChangeKind.MOVED and "from" in d:
        return f"🔁 Перенос: {title}\n    было: {_snap_when(d['from'])}\n    стало: {_snap_when(d['to'])}"
    if change.kind == ChangeKind.RESTORED:
        return f"✅ Снова в расписании: {title}, {format_date(lesson.date)}, {lesson.begins_at:%H:%M}"

    diff = d.get("changed", {})
    lines = [f"✏️ Изменение: {title}, {format_date(lesson.date)}, {lesson.begins_at:%H:%M}"]
    if {"date", "begins_at", "ends_at"} & diff.keys():
        old_date = diff.get("date", [lesson.date.isoformat()] * 2)[0]
        old_time = diff.get("begins_at", [lesson.begins_at.isoformat()] * 2)[0]
        lines[0] = f"🔁 Перенос: {title}"
        lines.append(f"    было: {_d(old_date)}, {_t(old_time)}")
        lines.append(f"    стало: {format_date(lesson.date)}, {lesson.begins_at:%H:%M}")
    for name, (old, new) in diff.items():
        if name in FIELD_TITLES:
            fmt = (lambda v: ", ".join(v) if isinstance(v, list) else (v or "—"))
            lines.append(f"    {FIELD_TITLES[name]}: {escape(fmt(old))} → <b>{escape(fmt(new))}</b>")
    return "\n".join(lines)


async def _cursor(session: AsyncSession, name: str) -> BotCursor:
    row = await session.get(BotCursor, name)
    if row is None:
        # Первый запуск: историю не рассылаем, начинаем с текущего конца журнала.
        last = await session.scalar(select(func.max(LessonChange.id))) or 0
        row = BotCursor(name=name, value=last)
        session.add(row)
        await session.flush()
    return row


async def collect_schedule_changes(session: AsyncSession, *, today: date) -> dict[int, list[str]]:
    """Новые записи журнала изменений → тексты по telegram_id. Курсор сдвигается (commit — у вызывающего)."""
    cursor = await _cursor(session, CHANGES_CURSOR)
    changes = (await session.scalars(
        select(LessonChange).where(LessonChange.id > cursor.value).order_by(LessonChange.id).limit(2000)
    )).all()
    if not changes:
        return {}
    cursor.value = changes[-1].id

    lesson_ids = {c.lesson_id for c in changes} | {c.previous_lesson_id for c in changes if c.previous_lesson_id}
    lessons = {row.id: row for row in (await session.scalars(select(Lesson).where(Lesson.id.in_(lesson_ids)))).all()}
    groups: dict[int, set[int]] = defaultdict(set)
    for lesson_id, group_id in (await session.execute(
        select(LessonGroup.lesson_id, LessonGroup.group_id).where(LessonGroup.lesson_id.in_(lesson_ids))
    )).all():
        groups[lesson_id].add(group_id)

    subscribers: dict[int, list[int]] = defaultdict(list)
    for telegram_id, group_id in (await session.execute(
        select(TelegramLink.telegram_id, Student.study_group_id)
        .join(Student, Student.id == TelegramLink.student_id)
        .where(TelegramLink.notify_changes.is_(True), Student.study_group_id.is_not(None))
    )).all():
        subscribers[group_id].append(telegram_id)

    out: dict[int, list[str]] = defaultdict(list)
    for change in changes:
        lesson = lessons.get(change.lesson_id)
        if lesson is None:
            continue
        relevant_dates = [lesson.date]
        if change.previous_lesson_id and change.previous_lesson_id in lessons:
            relevant_dates.append(lessons[change.previous_lesson_id].date)
        if not any(today <= d <= today + CHANGE_HORIZON for d in relevant_dates):
            continue
        affected = groups[change.lesson_id] | groups.get(change.previous_lesson_id or 0, set())
        text = describe_change(change, lesson)
        for group_id in affected:
            for telegram_id in subscribers.get(group_id, []):
                if text not in out[telegram_id]:
                    out[telegram_id].append(text)
    return dict(out)


def format_changes(texts: list[str]) -> str:
    return "<b>Изменения в расписании</b>\n\n" + "\n\n".join(texts) + "\n\nКалендарь по подписке обновится сам."


def describe_event(event: CertificateRequestEvent, request: CertificateRequest) -> str | None:
    title = escape(CertificateKind.TITLES.get(request.kind, "Справка"))
    head = f"📄 <b>{title}</b> (заявка №{request.id})"
    if event.kind == EventKind.STATUS and event.status:
        text = f"{head}\nСтатус: <b>{escape(RequestStatus.TITLES.get(event.status, event.status))}</b>"
        if event.status == RequestStatus.REJECTED and event.text:
            text += f"\nПричина: {escape(event.text)}"
        return text
    if event.kind == EventKind.DEAN_MESSAGE:
        return f"{head}\nДеканат пишет: «{escape(event.text or '')}»\nОтветить можно в кабинете, в карточке заявки."
    if event.kind == EventKind.PDF_READY:
        return f"{head}\n✅ Электронная справка готова — скачайте её в кабинете."
    if event.kind == EventKind.PAPER and event.status and event.status != PaperStatus.REQUESTED:
        return f"{head}\n{escape(PaperStatus.TITLES.get(event.status, event.status))}"
    return None


async def collect_certificate_events(session: AsyncSession) -> dict[int, list[str]]:
    """Неотправленные события заявок → тексты по telegram_id; всем строкам ставится notified_at."""
    rows = (await session.execute(
        select(CertificateRequestEvent, CertificateRequest)
        .join(CertificateRequest, CertificateRequest.id == CertificateRequestEvent.request_id)
        .where(CertificateRequestEvent.notified_at.is_(None))
        .order_by(CertificateRequestEvent.id)
        .limit(500)
    )).all()
    if not rows:
        return {}
    student_ids = {req.student_id for _, req in rows}
    links = {link.student_id: link for link in (await session.scalars(
        select(TelegramLink).where(TelegramLink.student_id.in_(student_ids))
    )).all()}
    out: dict[int, list[str]] = defaultdict(list)
    now = utcnow()
    for event, req in rows:
        event.notified_at = now
        link = links.get(req.student_id)
        if link is None or not link.notify_certificates:
            continue
        text = describe_event(event, req)
        if text:
            out[link.telegram_id].append(text)
    return dict(out)


async def mark_once(session: AsyncSession, student_id: int, key: str) -> bool:
    """True, если такого напоминания ещё не было (и теперь оно записано)."""
    exists = await session.scalar(select(BotNotice.id).where(BotNotice.student_id == student_id, BotNotice.key == key))
    if exists:
        return False
    session.add(BotNotice(student_id=student_id, key=key, sent_at=utcnow()))
    await session.flush()
    return True
