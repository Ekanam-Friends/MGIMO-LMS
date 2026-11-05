"""Точка входа бота: `python -m app.bot.main`.

Long polling: публичный адрес боту не нужен, его можно запустить на любой машине.
Токен — только из TELEGRAM_BOT_TOKEN в .env. Миграции применяет контейнер web;
бот стартует после него (см. docker-compose.yml).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import BotCommand

from app.bot import scheduler
from app.bot.handlers import router
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory

log = logging.getLogger(__name__)

COMMANDS = [
    ("today", "Занятия на сегодня"),
    ("tomorrow", "Занятия на завтра"),
    ("week", "Расписание на неделю"),
    ("exams", "Зачёты, экзамены, контрольные"),
    ("calendar", "Расписание в календарь телефона"),
    ("certificates", "Статусы справок"),
    ("settings", "Сводка, уведомления, напоминания"),
    ("help", "Что умеет бот"),
]


async def with_retries(action, *, what: str, attempts: int = 6):
    """Из России api.telegram.org доступен с перебоями — не падаем на первом обрыве."""
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except TelegramNetworkError as exc:
            if attempt == attempts:
                raise
            delay = min(2**attempt, 30)
            log.warning("%s: нет связи с Telegram (%s), повтор через %d с", what, exc, delay)
            await asyncio.sleep(delay)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env — бот не запускается")

    session = AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None
    bot = Bot(
        token=settings.telegram_bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    engine = make_engine(settings.database_url)
    sf = make_session_factory(engine)

    dp = Dispatcher()
    dp["sf"] = sf
    dp["settings"] = settings
    dp.include_router(router)

    try:
        me = await with_retries(bot.get_me, what="проверка токена")
        await with_retries(
            lambda: bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS]),
            what="регистрация команд",
        )
    except TelegramNetworkError:
        log.error("Telegram недоступен. Если так постоянно — задайте TELEGRAM_PROXY в .env")
        await bot.session.close()
        raise SystemExit(1) from None
    if settings.telegram_bot_username and settings.telegram_bot_username.lower() != (me.username or "").lower():
        log.warning("TELEGRAM_BOT_USERNAME=%s, а токен от @%s — ссылка из профиля будет вести не туда",
                    settings.telegram_bot_username, me.username)

    tasks = scheduler.start(scheduler.Context(bot=bot, settings=settings, sf=sf))
    log.info("Бот @%s запущен", me.username)
    try:
        # Накопленные за простой апдейты не обрабатываем: вчерашний /today сегодня только путает.
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        for task in tasks:
            task.cancel()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
