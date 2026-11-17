"""Фоновые задачи бота — asyncio-задачи рядом с polling, как в ikanam_schedule_bot.

* раз в минуту: изменения расписания, события по справкам, утренняя сводка, напоминания
  о контрольных точках;
* при старте (если задан BOT_BOOTSTRAP_GROUPS): догрузить весь семестр, пока расписание не
  наберётся у N групп — чтобы после выкладки на пустую базу кабинет не оставался без расписания;
* раз в BOT_IMPORT_HOURS часов: перечитать РУЗ (окно −7…+35 дней) по группам подключённых
  студентов или, при BOT_IMPORT_SCOPE=students, всех студентов в базе. РУЗ чужой сервер,
  полный обход всех 1534 групп остаётся ручной командой (решения 0002, 0005, 0011).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import notify
from app.bot.document_notify import deliver_document_notifications
from app.bot.formatting import clip, format_date, format_day, short_kind
from app.bot.handlers import MOSCOW, upcoming_exams
from app.core.config import Settings
from app.models import GroupScheduleCoverage, ImportRun, Lesson, LessonGroup, Student, StudyGroup, TelegramLink
from app.models.schedule import ImportStatus, LessonStatus
from app.web.schedule_view import lessons_for

log = logging.getLogger(__name__)

EXAM_REMINDER_FROM_HOUR = 10
"""Напоминания о зачётах — не раньше 10:00: в 7 утра это будильник, а не напоминание."""
DIGEST_LATEST_HOUR = 12
"""Если бот лежал утром, сводку после полудня уже не шлём: она про «сегодня с утра»."""


@dataclass
class Context:
    bot: Bot
    settings: Settings
    sf: async_sessionmaker[AsyncSession]


async def send(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, clip(text))
        return True
    except TelegramForbiddenError:
        log.info("чат %s закрыл бота — пропускаем", chat_id)
    except TelegramAPIError as exc:
        log.warning("не отправилось в %s: %s", chat_id, exc)
    return False


def digest_due(link: TelegramLink, now_msk: datetime) -> bool:
    if not link.digest_at or link.digest_sent_on == now_msk.date():
        return False
    if now_msk.hour >= DIGEST_LATEST_HOUR:
        return False
    hh, mm = (int(x) for x in link.digest_at.split(":"))
    return (now_msk.hour, now_msk.minute) >= (hh, mm)


async def tick(ctx: Context, *, now: datetime | None = None) -> None:
    now_msk = (now or datetime.now(MOSCOW)).astimezone(MOSCOW)
    today = now_msk.date()
    await deliver_document_notifications(ctx.bot, ctx.sf)

    async with ctx.sf() as session:
        changes = await notify.collect_schedule_changes(session, today=today)
        events = await notify.collect_certificate_events(session)
        await session.commit()
    for chat_id, texts in changes.items():
        await send(ctx.bot, chat_id, notify.format_changes(texts))
    for chat_id, texts in events.items():
        await send(ctx.bot, chat_id, "\n\n".join(texts))

    async with ctx.sf() as session:
        rows = (await session.execute(
            select(TelegramLink, Student).join(Student, Student.id == TelegramLink.student_id)
        )).all()
        for link, student in rows:
            if digest_due(link, now_msk):
                link.digest_sent_on = today
                await session.commit()
                days = await lessons_for(session, student.study_group_id, today, today)
                if days.get(today):
                    # Без занятий сводка — будильник без причины, молчим.
                    await send(ctx.bot, link.telegram_id,
                               format_day(today, days[today], title=f"Доброе утро! Сегодня, {format_date(today)}"))
            if link.exam_reminder_days and now_msk.hour >= EXAM_REMINDER_FROM_HOUR:
                await _exam_reminders(ctx, session, link, student, today)


async def _exam_reminders(ctx: Context, session: AsyncSession, link: TelegramLink, student: Student, today: date) -> None:
    for day, slot, lesson in await upcoming_exams(session, student.study_group_id, today, days=link.exam_reminder_days):
        left = (day - today).days
        if left <= 0:
            continue
        if not await notify.mark_once(session, student.id, f"exam:{lesson.id}:{day.isoformat()}"):
            continue
        await session.commit()
        when = "завтра" if left == 1 else f"через {left} дн."
        place = f", ауд. {escape(lesson.auditorium)}" if lesson.auditorium else ""
        await send(ctx.bot, link.telegram_id,
                   f"🎯 <b>{when.capitalize()}</b> — {escape(lesson.discipline)} ({escape(short_kind(lesson.kind))})\n"
                   f"{format_date(day)}, {slot.begins_at:%H:%M}{place}")


async def run_tick_loop(ctx: Context) -> None:
    while True:
        try:
            await tick(ctx)
        except Exception:  # noqa: BLE001 — петля не должна умирать из-за одной ошибки
            log.exception("фоновая проверка упала")
        now = datetime.now(MOSCOW)
        await asyncio.sleep(60 - now.second + 0.5)


BOOTSTRAP_MAX_COURSE = 6
"""Догружаем только группы с курсом 1–6: бакалавриат, специалитет, магистратура, без аспирантуры."""


async def refresh_group_ids(session: AsyncSession, scope: str) -> set[int]:
    """Группы для периодического обновления: студентов с Telegram или всех студентов в базе."""
    q = select(Student.study_group_id).distinct().where(Student.study_group_id.is_not(None))
    if scope != "students":
        q = q.join(TelegramLink, TelegramLink.student_id == Student.id)
    return set((await session.scalars(q)).all())


async def loaded_group_ids(session: AsyncSession, *, today: date | None = None) -> set[int]:
    """Группы курсов 1–6 с подтверждённым покрытием текущего семестра и занятиями."""
    from app.schedule.importer import semester_bounds

    start, finish = semester_bounds(today or datetime.now(MOSCOW).date())
    q = (
        select(GroupScheduleCoverage.group_id).distinct()
        .join(ImportRun, ImportRun.id == GroupScheduleCoverage.run_id)
        .join(StudyGroup, StudyGroup.id == GroupScheduleCoverage.group_id)
        .join(LessonGroup, LessonGroup.group_id == GroupScheduleCoverage.group_id)
        .join(Lesson, Lesson.id == LessonGroup.lesson_id)
        .where(
            GroupScheduleCoverage.period_start <= start, GroupScheduleCoverage.period_end >= finish,
            ImportRun.status.in_([ImportStatus.OK, ImportStatus.PARTIAL]),
            StudyGroup.course.between(1, BOOTSTRAP_MAX_COURSE),
            Lesson.date.between(start, finish), Lesson.status == LessonStatus.ACTIVE,
        )
    )
    return set((await session.scalars(q)).all())


async def bootstrap_schedule(ctx: Context, *, transport: object | None = None) -> int:
    """Догружает весь семестр новых групп, пока расписание не наберётся у BOT_BOOTSTRAP_GROUPS.

    Возвращает число непустых групп. Группы с подтверждённым семестром не трогает: повторный
    старт бота после успешной загрузки — один запрос к своей базе и ни одного к РУЗ.
    Добавленные при расширении окна занятия в журнал не попадают, пушей о них не будет.
    """
    from app.schedule.importer import ScheduleImporter, semester_bounds
    from app.schedule.ruz_client import RuzClient

    target = ctx.settings.bot_bootstrap_groups
    if target <= 0:
        return 0
    today = datetime.now(MOSCOW).date()
    async with ctx.sf() as session:
        have = await loaded_group_ids(session, today=today)
    missing = target - len(have)
    if missing <= 0:
        log.info("расписание уже есть у %s групп (нужно %s) — догрузка не нужна", len(have), target)
        return 0
    start, finish = semester_bounds(today)
    log.info("расписание есть у %s групп из %s — догружаю семестр ещё %s групп", len(have), target, missing)
    cfg = ctx.settings
    extra = {"transport": transport} if transport is not None else {}  # подмена РУЗ в тестах
    async with RuzClient(cfg.ruz_base_url, delay=cfg.ruz_request_delay, timeout=cfg.ruz_timeout,
                         verify_tls=cfg.ruz_verify_tls, **extra) as client:
        importer = ScheduleImporter(ctx.sf, client, fake_name_salt=cfg.fake_name_salt,
                                    concurrency=cfg.ruz_concurrency, report_additions=False)
        stats = await importer.run(
            start, finish, nonempty_limit=missing,
            group_filter=lambda g: g.id not in have and g.course is not None and 1 <= g.course <= BOOTSTRAP_MAX_COURSE,
        )
    log.info("догрузка расписания: %s, групп %s/%s, занятий %s", stats.status, stats.groups_ok, stats.groups_total,
             stats.lessons_seen)
    if stats.groups_populated < missing:
        log.warning("цель догрузки не достигнута: %s/%s групп; кандидаты исчерпаны",
                    len(have) + stats.groups_populated, target)
    return stats.groups_populated


async def import_linked_groups(ctx: Context) -> None:
    """Импорт РУЗ (окно −7…+35 дней) по группам из BOT_IMPORT_SCOPE."""
    from app.schedule.import_ruz import DAILY_AHEAD, DAILY_BACK
    from app.schedule.importer import ScheduleImporter
    from app.schedule.ruz_client import RuzClient

    async with ctx.sf() as session:
        group_ids = await refresh_group_ids(session, ctx.settings.bot_import_scope)
    if not group_ids:
        return
    today = datetime.now(MOSCOW).date()
    cfg = ctx.settings
    async with RuzClient(cfg.ruz_base_url, delay=cfg.ruz_request_delay, timeout=cfg.ruz_timeout,
                         verify_tls=cfg.ruz_verify_tls) as client:
        importer = ScheduleImporter(ctx.sf, client, fake_name_salt=cfg.fake_name_salt, concurrency=cfg.ruz_concurrency)
        stats = await importer.run(today - DAILY_BACK, today + DAILY_AHEAD, group_filter=lambda g: g.id in group_ids)
    log.info("импорт РУЗ по %s группам: %s, изменения %s", len(group_ids), stats.status, stats.changes or "нет")


async def run_import_loop(ctx: Context) -> None:
    await asyncio.sleep(120)  # дать боту подняться; импорт — не срочно
    try:
        await bootstrap_schedule(ctx)
    except Exception:  # noqa: BLE001 — без расписания бот всё равно работает
        log.exception("догрузка расписания при старте упала")
    hours = ctx.settings.bot_import_hours
    if hours <= 0:
        return
    while True:
        try:
            await import_linked_groups(ctx)
        except Exception:  # noqa: BLE001
            log.exception("импорт РУЗ из бота упал")
        await asyncio.sleep(timedelta(hours=hours).total_seconds())


def start(ctx: Context) -> list[asyncio.Task]:
    return [asyncio.create_task(run_tick_loop(ctx)), asyncio.create_task(run_import_loop(ctx))]
