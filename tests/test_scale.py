"""Расширение без потерь, повторный запуск и реальное покрытие преподавателей."""
from datetime import date, datetime

import pytest
from sqlalchemy import func, select, update

from app.auth.service import authenticate, hash_password, utcnow
from app.generate.run import generate
from app.generate.scale import prepare
from app.models import Lesson, Score, Student
from app.models.auth import User
from app.models.schedule import GroupScheduleCoverage, ImportRun
from tests.test_generate import TODAY, _db


async def test_scale_extends_without_reset_and_accounts_can_authenticate(tmp_path):
    url, engine, sf = await _db(tmp_path)
    try:
        async with sf() as db:
            db.add(ImportRun(id=1, started_at=datetime.combine(TODAY, datetime.min.time()),
                finished_at=datetime.combine(TODAY, datetime.min.time()), period_start=date(2026, 9, 1),
                period_end=date(2027, 1, 31), status="ok", groups_total=1, groups_ok=1, groups_failed=0, lessons_seen=7))
            await db.flush()
            db.add(GroupScheduleCoverage(group_id=10, period_start=date(2026, 9, 1), period_end=date(2027, 1, 31),
                                        run_id=1))
            db.add(User(login="existing-demo", role="student", student_id=500,
                        password_hash=hash_password("demo-only"), created_at=utcnow()))
            await db.execute(update(Lesson).values(last_seen_at=datetime.combine(TODAY, datetime.min.time())))
            await db.commit()
        await generate(students=2, per_group=2, seed="t", do_reset=False, only=[], today=TODAY,
                       database_url=url, demo_password="", access_password="demo-only")
        async with sf() as db:
            score = await db.scalar(select(Score).limit(1))
            score.points = 3.5
            original_id = score.id
            await db.commit()
        result = await prepare(students=8, teachers=4, staff=2, apply=True, demo_access=True,
                               database_url=url, password="demo-only", today=TODAY)
        assert result["ready"] and result["teachers_with_students_and_schedule"] == 4
        async with sf() as db:
            assert (await db.get(Score, original_id)).points == 3.5
            logins = list((await db.scalars(select(User.login))).all())
            for login in logins:
                assert await authenticate(db, login, "demo-only") is not None
            count = await db.scalar(select(func.count()).select_from(Student))
            scores = await db.scalar(select(func.count()).select_from(Score))
        again = await prepare(students=8, teachers=4, staff=2, apply=True, demo_access=True,
                              database_url=url, password="demo-only", today=TODAY)
        assert again["ready"]
        async with sf() as db:
            assert await db.scalar(select(func.count()).select_from(Student)) == count
            assert await db.scalar(select(func.count()).select_from(Score)) == scores
            assert (await db.get(Score, original_id)).points == 3.5
    finally:
        await engine.dispose()


async def test_scale_check_does_not_write_and_insufficient_ruz_stops_before_generation(tmp_path):
    url, engine, sf = await _db(tmp_path)
    try:
        report = await prepare(students=10000, teachers=800, database_url=url, password="demo", today=TODAY)
        assert not report["ready"] and report["students"] == 1
        with pytest.raises(ValueError, match="покрытие РУЗ"):
            await prepare(students=10000, teachers=800, apply=True, demo_access=True,
                          database_url=url, password="demo", today=TODAY)
        async with sf() as db:
            assert await db.scalar(select(func.count()).select_from(Student)) == 1
        with pytest.raises(ValueError, match="demo-access"):
            await prepare(apply=True, database_url=url, password="demo", today=TODAY)
    finally:
        await engine.dispose()


async def test_short_window_is_not_semester_coverage(tmp_path):
    url, engine, sf = await _db(tmp_path)
    try:
        now = datetime.combine(TODAY, datetime.min.time())
        async with sf() as db:
            db.add(ImportRun(id=1, started_at=now, finished_at=now, period_start=TODAY,
                            period_end=TODAY, status="ok"))
            await db.flush()
            db.add(GroupScheduleCoverage(group_id=10, period_start=TODAY, period_end=TODAY,
                                         run_id=1))
            await db.execute(update(Lesson).values(last_seen_at=now))
            await db.commit()
        report = await prepare(students=1, teachers=1, staff=1, database_url=url, today=TODAY)
        assert report["eligible_groups"] == 0 and not report["ready"]
    finally:
        await engine.dispose()
