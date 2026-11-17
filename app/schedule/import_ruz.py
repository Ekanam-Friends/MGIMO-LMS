"""Импорт расписания из РУЗ МГИМО.

    python -m app.schedule.import_ruz --full                    # весь текущий семестр, все группы
    python -m app.schedule.import_ruz                           # окно −7…+35 дней (ежедневный запуск)
    python -m app.schedule.import_ruz --group 1МО-01 --group 1МО-02
    python -m app.schedule.import_ruz --faculty 24 --limit 10

Перед первым запуском: `alembic upgrade head`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, timedelta

from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.models.schedule import ImportStatus
from app.schedule.importer import ScheduleImporter, semester_bounds
from app.schedule.normalize import GroupData
from app.schedule.ruz_client import RuzClient

DAILY_BACK = timedelta(days=7)
DAILY_AHEAD = timedelta(days=35)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Импорт расписания из РУЗ МГИМО")
    period = parser.add_mutually_exclusive_group()
    period.add_argument("--full", action="store_true", help="весь текущий семестр")
    period.add_argument("--start", type=date.fromisoformat, help="начало периода, ГГГГ-ММ-ДД")
    parser.add_argument("--finish", type=date.fromisoformat, help="конец периода, ГГГГ-ММ-ДД")
    parser.add_argument("--group", action="append", default=[], help="имя группы (1МО-01 или 26МО-01)")
    parser.add_argument("--faculty", action="append", type=int, default=[], help="oid факультета")
    parser.add_argument("--limit", type=int, help="не больше N групп (для проверки)")
    args = parser.parse_args(argv)
    if (args.start is None) != (args.finish is None):
        parser.error("--start и --finish задаются вместе")
    return args


def _period(args: argparse.Namespace, today: date) -> tuple[date, date]:
    if args.full:
        return semester_bounds(today)
    if args.start:
        return args.start, args.finish
    return today - DAILY_BACK, today + DAILY_AHEAD


def _group_filter(args: argparse.Namespace):
    names = {n.strip().upper() for n in args.group}
    faculties = set(args.faculty)
    if not names and not faculties:
        return None

    def accept(group: GroupData) -> bool:
        if names and not ({group.name.upper(), group.number.upper()} & names):
            return False
        return not faculties or group.faculty_id in faculties

    return accept


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    settings = get_settings()
    start, finish = _period(args, date.today())
    logging.info("период %s — %s", start, finish)

    engine = make_engine(settings.database_url)
    try:
        async with RuzClient(
            settings.ruz_base_url, delay=settings.ruz_request_delay, timeout=settings.ruz_timeout,
            verify_tls=settings.ruz_verify_tls,
        ) as client:
            importer = ScheduleImporter(
                make_session_factory(engine),
                client,
                fake_name_salt=settings.fake_name_salt,
                concurrency=settings.ruz_concurrency,
            )
            stats = await importer.run(start, finish, group_filter=_group_filter(args), limit=args.limit)
    finally:
        await engine.dispose()

    logging.info(
        "импорт #%s: %s; групп %s/%s, ошибок %s; занятий %s; изменения %s",
        stats.run_id,
        stats.status,
        stats.groups_ok,
        stats.groups_total,
        stats.groups_failed,
        stats.lessons_seen,
        stats.changes or "нет",
    )
    if stats.failed_groups:
        logging.warning("не загрузились: %s", ", ".join(stats.failed_groups[:30]))
    return 0 if stats.status in (ImportStatus.OK, ImportStatus.PARTIAL) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
