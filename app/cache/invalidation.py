"""Инвалидация кэша по факту записи в БД — в любом процессе (web, бот, CLI импорта).

Мест записи много (импорт РУЗ, демо-перенос, журнал оценок, сверка с Pyrus, объявления,
уведомления о документах, отметки «прочитано»), и новые появляются. Поэтому поколения
сбрасываются не вызовами в каждом сервисе, а слушателями сессии SQLAlchemy:

1. `after_flush` и `do_orm_execute` запоминают, в какие таблицы из карты ниже шла запись
   (ORM-объекты и `insert/update/delete` через сессию);
2. `after_commit` внешней транзакции — уже после успешного COMMIT — меняет поколения.
   Коммит точки сохранения (`begin_nested`) не считается: данные ещё не видны другим.

Откат не очищает список: лишняя инвалидация безопасна, пропущенная — нет.
Не отслеживаются сырые `text()`-запросы и запись мимо сессии (engine.connect) — в проекте
таких записей в эти таблицы нет; если появятся, нужен явный `get_cache().bump(...)`.
"""

from __future__ import annotations

import logging
from itertools import chain

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.util.concurrency import await_only, in_greenlet

from app.cache.core import get_cache

log = logging.getLogger(__name__)

SCHEDULE = "schedule"
NOTIFICATIONS = "notifications"


def calendar_of(student_id: int) -> str:
    """Личный слой календаря студента: выбор элективов и запись на активности."""
    return f"calendar:s{student_id}"


def reads_of(user_id: int) -> str:
    """Отметки «прочитано» пользователя."""
    return f"notifications:u{user_id}"


SCHEDULE_TABLES = {"lessons", "lesson_groups", "lesson_teachers", "teachers", "study_groups", "activities"}
"""Общие данные расписания: меняет импорт РУЗ, демо-перенос, seed."""
CALENDAR_TABLES = {"elective_choices": "student_id", "activity_signups": "student_id"}
"""Личный слой календаря → поле со студентом. Без него (массовый DML) — общее поколение расписания."""
NOTIFICATION_TABLES = {
    "announcements", "certificate_request_events", "grade_events", "document_notices",
    "document_recipients", "courses", "disciplines", "students",
    "certificate_requests",  # dean_message в тексте «нужны сведения»; сверка Pyrus очищает его без события
    "knowledge_documents",  # заголовок и ссылка уведомления о документе
}
"""Источники ленты колокольчика (app/notifications/service.py) и данные адресации объявлений."""
READ_TABLES = {"notification_reads": "user_id"}

_PENDING = "lms_cache_bumps"


def _mark(pending: set[str], table: str, obj: object | None = None) -> None:
    if table in SCHEDULE_TABLES:
        pending.add(SCHEDULE)
    elif table in CALENDAR_TABLES:
        sid = getattr(obj, CALENDAR_TABLES[table], None) if obj is not None else None
        pending.add(calendar_of(sid) if isinstance(sid, int) else SCHEDULE)
    elif table in NOTIFICATION_TABLES:
        pending.add(NOTIFICATIONS)
    elif table in READ_TABLES:
        uid = getattr(obj, READ_TABLES[table], None) if obj is not None else None
        pending.add(reads_of(uid) if isinstance(uid, int) else NOTIFICATIONS)


def _table_of(obj: object) -> str | None:
    try:
        return inspect(obj).mapper.local_table.name
    except Exception:  # noqa: BLE001
        return None


@event.listens_for(Session, "after_flush")
def _collect_flush(session: Session, _ctx) -> None:
    pending = session.info.setdefault(_PENDING, set())
    for obj in chain(session.new, session.deleted):
        if table := _table_of(obj):
            _mark(pending, table, obj)
    for obj in session.dirty:
        if (table := _table_of(obj)) and session.is_modified(obj):
            _mark(pending, table, obj)


@event.listens_for(Session, "do_orm_execute")
def _collect_dml(state) -> None:
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    table = getattr(state.statement, "table", None)
    name = getattr(table, "name", None)
    if name:
        _mark(state.session.info.setdefault(_PENDING, set()), name)


@event.listens_for(Session, "after_commit")
def _bump_after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return
    names = session.info.pop(_PENDING, None)
    if not names:
        return
    if not in_greenlet():
        # Синхронная сессия (миграции, run_sync): дождаться async-кэша отсюда нельзя.
        log.warning("кэш: запись вне AsyncSession, поколения %s не сброшены — сработает TTL", sorted(names))
        return
    try:
        await_only(get_cache().bump(*sorted(names)))
    except Exception:  # noqa: BLE001 — коммит уже прошёл, ошибка кэша не должна его «отменять»
        log.warning("кэш: не удалось сбросить поколения %s", sorted(names))
