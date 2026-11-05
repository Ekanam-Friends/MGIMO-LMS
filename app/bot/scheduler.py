"""Фоновые задачи бота — asyncio-задачи рядом с polling, как в ikanam_schedule_bot.

* раз в минуту: изменения расписания, события по справкам, утренняя сводка, напоминания
  о контрольных точках;
* раз в BOT_IMPORT_HOURS часов: перечитать РУЗ, но только по группам подключённых
  студентов — РУЗ чужой сервер, полный обход всех групп остаётся ручной командой.
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
from app.bot.formatting import clip, format_date, format_day, short_kind
from app.bot.handlers import MOSCOW, upcoming_exams
from app.core.config import Settings
from app.models import Student, TelegramLink
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


async def import_linked_groups(ctx: Context) -> None:
    """Импорт РУЗ (окно −7…+35 дней) только по группам подключённых студентов."""
    from app.schedule.import_ruz import DAILY_AHEAD, DAILY_BACK
    from app.schedule.importer import ScheduleImporter
    from app.schedule.ruz_client import RuzClient

    async with ctx.sf() as session:
        group_ids = set((await session.scalars(
            select(Student.study_group_id).join(TelegramLink, TelegramLink.student_id == Student.id)
            .where(Student.study_group_id.is_not(None))
        )).all())
    if not group_ids:
        return
    today = datetime.now(MOSCOW).date()
    cfg = ctx.settings
    async with RuzClient(cfg.ruz_base_url, delay=cfg.ruz_request_delay, timeout=cfg.ruz_timeout) as client:
        importer = ScheduleImporter(ctx.sf, client, fake_name_salt=cfg.fake_name_salt, concurrency=cfg.ruz_concurrency)
        stats = await importer.run(today - DAILY_BACK, today + DAILY_AHEAD, group_filter=lambda g: g.id in group_ids)
    log.info("импорт РУЗ по %s группам: %s, изменения %s", len(group_ids), stats.status, stats.changes or "нет")


async def run_import_loop(ctx: Context) -> None:
    hours = ctx.settings.bot_import_hours
    if hours <= 0:
        return
    await asyncio.sleep(120)  # дать боту подняться; импорт — не срочно
    while True:
        try:
            await import_linked_groups(ctx)
        except Exception:  # noqa: BLE001
            log.exception("импорт РУЗ из бота упал")
        await asyncio.sleep(timedelta(hours=hours).total_seconds())


def start(ctx: Context) -> list[asyncio.Task]:
    return [asyncio.create_task(run_tick_loop(ctx)), asyncio.create_task(run_import_loop(ctx))]
