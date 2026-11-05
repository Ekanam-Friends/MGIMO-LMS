from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import select

from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.models import ImportRun, Lesson, LessonChange, LessonGroup, Teacher
from app.models.schedule import ChangeKind, ImportStatus, LessonStatus
from app.schedule.importer import ScheduleImporter, semester_bounds
from app.schedule.ruz_client import RuzClient
from tests.fake_ruz import REAL_NAME_MARKERS, FakeRuz, group, lesson

START, FINISH = date(2026, 11, 2), date(2026, 11, 8)
A, B = 7001, 7002
MON, TUE, WED, THU = (date(2026, 11, d) for d in (2, 3, 4, 5))


@pytest.fixture
async def sf(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def ruz():
    fake = FakeRuz()
    fake.groups = [group(A, "1МО-01", "26МО-01"), group(B, "1МО-02", "26МО-02")]
    return fake


async def run_import(sf, ruz: FakeRuz, *, concurrency: int = 2, **kwargs):
    async with RuzClient("https://ruz.test/api/", delay=0, backoff=0, retries=1, transport=ruz.transport()) as client:
        importer = ScheduleImporter(sf, client, fake_name_salt="test", concurrency=concurrency)
        return await importer.run(START, FINISH, **kwargs)


async def changes(sf, run_id: int) -> list[LessonChange]:
    async with sf() as s:
        return list((await s.scalars(select(LessonChange).where(LessonChange.run_id == run_id))).all())


async def lesson_row(sf, oid: int) -> Lesson:
    async with sf() as s:
        return await s.get(Lesson, oid)


async def groups_of(sf, oid: int) -> set[int]:
    async with sf() as s:
        return set((await s.scalars(select(LessonGroup.group_id).where(LessonGroup.lesson_id == oid))).all())


async def dump_db(sf) -> str:
    parts = []
    async with sf() as s:
        for table in Base.metadata.sorted_tables:
            for row in (await s.execute(select(table))).all():
                parts.append(repr(tuple(row)))
    return "\n".join(parts)


def base_schedule(ruz: FakeRuz) -> None:
    shared = lesson(1, MON)
    ruz.schedules = {
        A: [shared, lesson(2, MON, "10:40", "12:00", kind="Семинар"), lesson(4, TUE, discipline="Логика"),
            lesson(5, TUE, "12:20", "13:40", discipline="Риторика", kind="Семинар")],
        B: [shared, lesson(3, WED, discipline="Право", lecturer_oid=502, lecturer=REAL_NAME_MARKERS[1])],
    }


async def test_initial_import_stores_lessons_without_real_names(sf, ruz):
    base_schedule(ruz)
    stats = await run_import(sf, ruz)

    assert stats.status == ImportStatus.OK
    assert (stats.groups_ok, stats.groups_failed) == (2, 0)
    assert stats.changes == {}, "первый импорт не должен засорять журнал «добавлено»"

    first = await lesson_row(sf, 1)
    assert (first.date, first.begins_at, first.auditorium) == (MON, time(9, 0), "101")
    assert await groups_of(sf, 1) == {A, B}
    assert await groups_of(sf, 3) == {B}

    async with sf() as s:
        teachers = (await s.scalars(select(Teacher))).all()
    assert {t.ruz_oid for t in teachers} == {501, 502}
    assert len({t.full_name for t in teachers}) == 2

    dump = await dump_db(sf)
    for marker in REAL_NAME_MARKERS:
        assert marker not in dump


async def test_second_run_logs_changes(sf, ruz):
    base_schedule(ruz)
    await run_import(sf, ruz)

    moved_same_oid = lesson(1, MON, "13:00", "14:20")
    ruz.schedules = {
        A: [
            moved_same_oid,
            lesson(2, MON, "10:40", "12:00", kind="Семинар", auditorium="202"),  # сменилась аудитория
            # 4 исчез — отмена
            # 5 исчез, вместо него 6 той же дисциплины в четверг — перенос
            lesson(6, THU, "12:20", "13:40", discipline="Риторика", kind="Семинар"),
            lesson(7, THU, "15:00", "16:20", discipline="Новая дисциплина"),  # добавлено
        ],
        B: [moved_same_oid, lesson(3, WED, discipline="Право", lecturer_oid=502)],
    }
    stats = await run_import(sf, ruz)
    assert stats.status == ImportStatus.OK

    by_lesson = {(c.kind, c.lesson_id): c for c in await changes(sf, stats.run_id)}
    assert set(by_lesson) == {
        (ChangeKind.MOVED, 1),
        (ChangeKind.CHANGED, 2),
        (ChangeKind.CANCELLED, 4),
        (ChangeKind.MOVED, 6),
        (ChangeKind.ADDED, 7),
    }
    assert by_lesson[(ChangeKind.CHANGED, 2)].details["changed"] == {"auditorium": ["101", "202"]}
    assert by_lesson[(ChangeKind.MOVED, 1)].details["changed"]["begins_at"] == ["09:00:00", "13:00:00"]
    pair = by_lesson[(ChangeKind.MOVED, 6)]
    assert pair.previous_lesson_id == 5
    assert pair.details["from"]["date"] == TUE.isoformat()

    assert (await lesson_row(sf, 4)).status == LessonStatus.CANCELLED
    old = await lesson_row(sf, 5)
    assert (old.status, old.moved_to_id) == (LessonStatus.CANCELLED, 6)
    assert (await lesson_row(sf, 3)).status == LessonStatus.ACTIVE


async def test_group_loaded_for_the_first_time_is_not_logged_as_added(sf, ruz):
    # Регрессия: пробный импорт по одной группе покрывал весь период, и следующий полный
    # импорт записал бы все занятия остальных групп как «добавлено».
    base_schedule(ruz)
    await run_import(sf, ruz, group_filter=lambda g: g.id == A)

    ruz.schedules[A].append(lesson(8, THU, "15:00", "16:20", discipline="Новая у A"))
    stats = await run_import(sf, ruz)

    assert [(c.kind, c.lesson_id) for c in await changes(sf, stats.run_id)] == [(ChangeKind.ADDED, 8)]
    assert await groups_of(sf, 3) == {B}


async def test_sequential_and_parallel_give_same_result(sf, ruz, tmp_path):
    base_schedule(ruz)
    await run_import(sf, ruz, concurrency=1)
    async with sf() as s:
        sequential = sorted((await s.execute(select(LessonGroup.lesson_id, LessonGroup.group_id))).all())

    other = make_session_factory(make_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}"))
    async with other.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_import(other, ruz, concurrency=4)
    async with other() as s:
        parallel = sorted((await s.execute(select(LessonGroup.lesson_id, LessonGroup.group_id))).all())
    await other.kw["bind"].dispose()
    assert sequential == parallel


async def test_failed_group_does_not_cancel_its_lessons(sf, ruz):
    base_schedule(ruz)
    await run_import(sf, ruz)

    ruz.failing = {B}
    ruz.schedules[A] = [l for l in ruz.schedules[A] if l["lessonOid"] != 1]  # общая лекция пропала у A
    stats = await run_import(sf, ruz)

    assert stats.status == ImportStatus.PARTIAL
    assert stats.failed_groups == ["1МО-02"]
    assert (await lesson_row(sf, 3)).status == LessonStatus.ACTIVE
    # Лекцию 1 видит ещё и B, а B не загрузилась — значит, про отмену мы не знаем.
    assert (await lesson_row(sf, 1)).status == LessonStatus.ACTIVE
    assert not [c for c in await changes(sf, stats.run_id) if c.kind == ChangeKind.CANCELLED]


async def test_lesson_leaving_one_group_only_drops_link(sf, ruz):
    base_schedule(ruz)
    await run_import(sf, ruz)

    ruz.schedules[A] = [l for l in ruz.schedules[A] if l["lessonOid"] != 1]
    stats = await run_import(sf, ruz)

    assert await groups_of(sf, 1) == {B}
    assert (await lesson_row(sf, 1)).status == LessonStatus.ACTIVE
    assert await changes(sf, stats.run_id) == []


async def test_empty_response_is_failure_not_mass_cancel(sf, ruz):
    base_schedule(ruz)
    await run_import(sf, ruz)

    ruz.schedules[A] = []
    stats = await run_import(sf, ruz)

    assert stats.failed_groups == ["1МО-01"]
    assert (await lesson_row(sf, 2)).status == LessonStatus.ACTIVE
    assert await changes(sf, stats.run_id) == []


async def test_cancelled_lesson_can_be_restored(sf, ruz):
    base_schedule(ruz)
    await run_import(sf, ruz)
    full_a = list(ruz.schedules[A])

    ruz.schedules[A] = [l for l in full_a if l["lessonOid"] != 4]
    await run_import(sf, ruz)
    assert (await lesson_row(sf, 4)).status == LessonStatus.CANCELLED

    ruz.schedules[A] = full_a
    stats = await run_import(sf, ruz)
    assert [(c.kind, c.lesson_id) for c in await changes(sf, stats.run_id)] == [(ChangeKind.RESTORED, 4)]
    assert (await lesson_row(sf, 4)).status == LessonStatus.ACTIVE


async def test_teacher_change_is_logged_with_fake_names(sf, ruz):
    base_schedule(ruz)
    await run_import(sf, ruz)

    ruz.schedules[A][1] = lesson(2, MON, "10:40", "12:00", kind="Семинар", lecturer_oid=777,
                                 lecturer=REAL_NAME_MARKERS[1])
    stats = await run_import(sf, ruz)

    [change] = await changes(sf, stats.run_id)
    assert change.kind == ChangeKind.CHANGED
    old_names, new_names = change.details["changed"]["teachers"]
    async with sf() as s:
        names = {t.ruz_oid: t.full_name for t in (await s.scalars(select(Teacher))).all()}
    assert (old_names, new_names) == ([names[501]], [names[777]])
    assert REAL_NAME_MARKERS[1] not in await dump_db(sf)


async def test_fake_surnames_never_match_real_ones(sf, ruz):
    base_schedule(ruz)
    # Реальные фамилии, которые есть и в списке вымышленных, — в обоих родах.
    ruz.lecturers += [{"lecturerOid": 900 + i, "fio": f"{s} Тест Тестович"} for i, s in
                      enumerate(("Ильин", "Савина", "Покровская", "Орлов", "Юдина"))]
    async with sf() as s:
        s.add(Teacher(ruz_oid=501, full_name="Ильин Иван Иванович"))  # имя из старого импорта
        await s.commit()

    await run_import(sf, ruz)

    async with sf() as s:
        surnames = {t.full_name.split()[0] for t in (await s.scalars(select(Teacher))).all()}
    forbidden = {"Ильин", "Ильина", "Савин", "Савина", "Покровский", "Покровская", "Орлов", "Орлова",
                 "Юдин", "Юдина"}
    assert not surnames & forbidden


async def test_group_filter_and_limit(sf, ruz):
    base_schedule(ruz)
    stats = await run_import(sf, ruz, group_filter=lambda g: g.number == "1МО-02")
    assert stats.groups_total == 1
    assert ruz.requests.count("/api/schedule/group/7001") == 0

    stats = await run_import(sf, ruz, limit=1)
    assert stats.groups_total == 1


async def test_import_run_is_recorded(sf, ruz):
    base_schedule(ruz)
    stats = await run_import(sf, ruz)
    async with sf() as s:
        run = await s.get(ImportRun, stats.run_id)
    assert (run.status, run.period_start, run.period_end, run.lessons_seen) == (ImportStatus.OK, START, FINISH, 6)
    assert isinstance(run.finished_at, datetime)


@pytest.mark.parametrize(
    ("today", "bounds"),
    [
        (date(2026, 11, 5), (date(2026, 9, 1), date(2027, 1, 31))),
        (date(2027, 1, 10), (date(2026, 9, 1), date(2027, 1, 31))),
        (date(2027, 3, 1), (date(2027, 2, 1), date(2027, 6, 30))),
    ],
)
def test_semester_bounds(today, bounds):
    assert semester_bounds(today) == bounds
