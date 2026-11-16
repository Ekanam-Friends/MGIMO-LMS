"""Догрузка расписания ботом при старте и выбор групп для периодического обновления (решение 0011)."""

from __future__ import annotations

from datetime import date, datetime
import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
import httpx

from app.bot.scheduler import Context, bootstrap_schedule, loaded_group_ids, refresh_group_ids
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.models import GroupScheduleCoverage, ImportRun, Lesson, LessonChange, Student, StudyGroup, TelegramLink
from app.schedule.importer import ScheduleImporter, semester_bounds
from app.schedule.ruz_client import RuzClient
from tests.fake_ruz import FakeRuz, group, lesson

DAY = semester_bounds(date.today())[0].replace(day=15)


@pytest.fixture
async def sf(tmp_path):
    url = os.environ.get("TEST_SCHEDULE_DATABASE_URL")
    if url:
        # Только отдельная тестовая БД: фикстура очищает схему перед каждым сценарием.
        assert make_url(url).database == "pr35_test"
    engine = make_engine(url or f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}")
    async with engine.begin() as conn:
        if not url:
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        if url:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def ruz():
    fake = FakeRuz()
    fake.groups = [group(1, "1ТЕ-01", "26ТЕ-01"), group(2, "1ТЕ-02", "26ТЕ-02"), group(3, "1ТЕ-03", "26ТЕ-03"),
                   {**group(4, "АСП-01", "26АСП-01"), "course": 9}, group(5, "1ТЕ-05", "26ТЕ-05")]
    for gid in (1, 2, 3, 4, 5):
        fake.schedules[gid] = [lesson(gid * 100 + i, DAY, discipline=f"Дисциплина {i}") for i in range(3)]
    return fake


def ctx(sf, target: int, scope: str = "linked") -> Context:
    settings = Settings(database_url="sqlite+aiosqlite://", bot_bootstrap_groups=target, bot_import_scope=scope,
                        ruz_request_delay=0, ruz_concurrency=1, pyrus_login="", demo_password="")
    return Context(bot=None, settings=settings, sf=sf)  # type: ignore[arg-type]


def schedule_requests(ruz: FakeRuz) -> list[int]:
    return [int(p.rsplit("/", 1)[1]) for p in ruz.requests if "/schedule/group/" in p]


async def test_off_by_default_makes_no_requests(sf, ruz):
    assert await bootstrap_schedule(ctx(sf, 0), transport=ruz.transport()) == 0
    assert ruz.requests == []


async def test_fills_up_to_target_without_reloading_or_postgraduates(sf, ruz):
    assert await bootstrap_schedule(ctx(sf, 1), transport=ruz.transport()) == 1
    async with sf() as s:
        assert await loaded_group_ids(s) == {1}
    ruz.requests.clear()
    assert await bootstrap_schedule(ctx(sf, 3), transport=ruz.transport()) == 2
    assert sorted(schedule_requests(ruz)) == [2, 3]  # группа 1 уже есть, аспирантура (курс 9) не берётся
    async with sf() as s:
        assert await loaded_group_ids(s) == {1, 2, 3}
        # новые группы не попадают в журнал изменений — бот не разошлёт пуши о «добавленных» занятиях
        assert await s.scalar(select(func.count()).select_from(LessonChange)) == 0


async def test_enough_groups_means_no_ruz_traffic(sf, ruz):
    await bootstrap_schedule(ctx(sf, 2), transport=ruz.transport())
    ruz.requests.clear()
    assert await bootstrap_schedule(ctx(sf, 2), transport=ruz.transport()) == 0
    assert ruz.requests == []


async def test_refresh_scope(sf):
    async with sf() as s:
        s.add_all([StudyGroup(id=10, name="26А-01", number="1А-01"), StudyGroup(id=20, name="26А-02", number="1А-02")])
        await s.flush()
        s.add_all([Student(id=1, full_name="Тестова Анна", record_book="Т-1", study_group_id=10),
                   Student(id=2, full_name="Примеров Борис", record_book="Т-2", study_group_id=20)])
        await s.flush()
        s.add(TelegramLink(student_id=1, telegram_id=111, linked_at=datetime(2026, 11, 1), feed_token="t"))
        await s.commit()
        assert await refresh_group_ids(s, "linked") == {10}
        assert await refresh_group_ids(s, "students") == {10, 20}


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("empty_kind", ["empty", "invalid", "http"])
async def test_skips_empty_and_failed_candidates(sf, ruz, concurrency, empty_kind):
    ruz.schedules[1] = [] if empty_kind == "empty" else [{"lessonOid": 100}]
    if empty_kind == "http":
        original = ruz._handle

        def fail_first(request):
            if request.url.path.endswith("/schedule/group/1"):
                ruz.requests.append(request.url.path)
                return httpx.Response(404)
            return original(request)

        transport = httpx.MockTransport(fail_first)
    else:
        transport = ruz.transport()
    context = ctx(sf, 2)
    context.settings.ruz_concurrency = concurrency
    assert await bootstrap_schedule(context, transport=transport) == 2
    assert sorted(schedule_requests(ruz)) == [1, 2, 3]
    async with sf() as s:
        assert await loaded_group_ids(s) == {2, 3}
    ruz.requests.clear()
    assert await bootstrap_schedule(context, transport=ruz.transport()) == 0
    assert ruz.requests == []


async def test_all_empty_candidates_report_zero_and_warn(sf, ruz, caplog):
    ruz.schedules.clear()
    assert await bootstrap_schedule(ctx(sf, 2), transport=ruz.transport()) == 0
    assert sorted(schedule_requests(ruz)) == [1, 2, 3, 5]
    assert "цель догрузки не достигнута" in caplog.text


async def import_period(sf, ruz, start, finish, group_id=1):
    async with RuzClient("https://ruz.test/api/", delay=0, transport=ruz.transport()) as client:
        return await ScheduleImporter(sf, client, fake_name_salt="test").run(
            start, finish, group_filter=lambda g: g.id == group_id
        )


async def test_short_import_does_not_count_as_semester(sf, ruz):
    await import_period(sf, ruz, DAY, DAY)
    async with sf() as s:
        assert await loaded_group_ids(s) == set()
    ruz.schedules[1].append(lesson(199, DAY.replace(day=20)))
    ruz.requests.clear()
    assert await bootstrap_schedule(ctx(sf, 1), transport=ruz.transport()) == 1
    assert schedule_requests(ruz) == [1]
    async with sf() as s:
        assert await s.get(Lesson, 199) is not None
        assert await loaded_group_ids(s) == {1}
    # Ежедневное обновление не стирает подтверждение полного семестра.
    await import_period(sf, ruz, DAY, DAY)
    async with sf() as s:
        assert await loaded_group_ids(s) == {1}


async def test_previous_semester_does_not_count(sf, ruz):
    previous = DAY.replace(year=DAY.year - 1)
    ruz.schedules[1] = [lesson(199, previous)]
    await import_period(sf, ruz, *semester_bounds(previous))
    async with sf() as s:
        assert await loaded_group_ids(s) == set()
    ruz.schedules[1] = [lesson(200, DAY)]
    assert await bootstrap_schedule(ctx(sf, 1), transport=ruz.transport()) == 1
    async with sf() as s:
        assert await loaded_group_ids(s) == {1}


async def test_legacy_import_needs_confirmation_once(sf, ruz):
    await import_period(sf, ruz, *semester_bounds(DAY))
    async with sf() as s:
        coverage = await s.get(GroupScheduleCoverage, (1, semester_bounds(DAY)[0]))
        await s.delete(coverage)
        await s.commit()
    assert await bootstrap_schedule(ctx(sf, 1), transport=ruz.transport()) == 1
    ruz.requests.clear()
    assert await bootstrap_schedule(ctx(sf, 1), transport=ruz.transport()) == 0
    assert ruz.requests == []


async def test_failed_run_does_not_confirm_coverage(sf, ruz):
    await bootstrap_schedule(ctx(sf, 1), transport=ruz.transport())
    async with sf() as s:
        coverage = await s.get(GroupScheduleCoverage, (1, semester_bounds(DAY)[0]))
        run = await s.get(ImportRun, coverage.run_id)
        run.status = "failed"
        await s.commit()
        assert await loaded_group_ids(s) == set()


async def test_extending_partial_group_does_not_emit_added_notifications(sf, ruz):
    await import_period(sf, ruz, *semester_bounds(DAY), group_id=2)
    await import_period(sf, ruz, DAY, DAY)
    ruz.schedules[1].append(lesson(199, DAY.replace(day=20)))
    assert await bootstrap_schedule(ctx(sf, 2), transport=ruz.transport()) == 1
    async with sf() as s:
        assert await s.get(Lesson, 199) is not None
        assert await s.scalar(select(func.count()).select_from(LessonChange)) == 0
