"""Замер кэша чтений (задача #24, решение 0027): SQL-запросы и время на страницу до/после.

    python -m scripts.measure_cache            # sqlite во временной папке, кэш в памяти
    python -m scripts.measure_cache -n 200     # больше повторов
    python -m scripts.measure_cache --redis redis://localhost:6379/15   # то же на настоящем Redis

Только вымышленные данные и временная база: lms_dev и стенд не трогаются.
«Без кэша» — тот же код с бэкендом, который ничего не хранит (каждый запрос идёт в БД, как
до задачи #24). «С кэшем» — повторные открытия после первого (тёплый кэш).
Запросы идут в приложение внутри процесса (httpx ASGITransport), без сети.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import tempfile
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.auth.service import hash_password, utcnow
from app.cache import Cache, MemoryBackend, RedisBackend, set_cache
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.models import Activity, Announcement, Lesson, LessonGroup, Student, StudyGroup, Teacher, lesson_teachers
from app.models.auth import Role, User
from app.models.notifications import Audience, Source

PASSWORD = "measure-only-password"
WEEK = date(2026, 11, 16)
SCHEDULE_TABLES = ("lessons", "lesson_groups", "lesson_teachers", "activities", "elective_choices", "activity_signups")
PAGES = [
    ("Расписание /schedule", f"/schedule?week={WEEK.isoformat()}"),
    ("Главная /", f"/?d={WEEK.isoformat()}"),
    ("Колокольчик /notifications/badge", "/notifications/badge"),
]


class NullBackend:
    """Ничего не хранит: каждый запрос читает БД — поведение до кэша."""

    async def mget(self, keys):
        return [None] * len(keys)

    async def set(self, key, value, ttl, *, nx=False):
        return True

    async def set_many(self, items, ttl):
        pass

    async def aclose(self):
        pass


async def seed(url: str) -> None:
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    now = datetime(2026, 11, 1)
    slots = [(dtime(9), dtime(10, 20)), (dtime(10, 40), dtime(12)), (dtime(12, 20), dtime(13, 40)),
             (dtime(14, 30), dtime(15, 50)), (dtime(16, 10), dtime(17, 30))]
    async with sf() as s:
        s.add(StudyGroup(id=1, name="26ЗАМЕР-01", number="1ЗАМЕР-01"))
        s.add_all([Teacher(id=i, ruz_oid=90000 + i, full_name=f"Вымышленный Преподаватель {i}") for i in range(1, 21)])
        s.add(Student(id=1, full_name="Замерова Вера Тестовна", record_book="З-1", study_group_id=1,
                      faculty_name="Факультет замеров"))
        lesson_id = 0
        for week in range(-2, 3):
            for day in range(6):
                for begins, ends in slots:
                    lesson_id += 1
                    s.add(Lesson(id=lesson_id, date=WEEK + timedelta(days=7 * week + day), begins_at=begins,
                                 ends_at=ends, discipline=f"Дисциплина {lesson_id % 12}", kind_of_work="Семинар",
                                 auditorium=f"ауд. {100 + lesson_id % 40}", building="Корпус А",
                                 first_seen_at=now, last_seen_at=now))
        await s.flush()
        s.add_all([LessonGroup(lesson_id=i, group_id=1) for i in range(1, lesson_id + 1)])
        await s.execute(lesson_teachers.insert(), [{"lesson_id": i, "teacher_id": 1 + i % 20}
                                                   for i in range(1, lesson_id + 1)])
        s.add_all([Activity(title=f"Клуб {i}", starts_at=datetime.combine(WEEK + timedelta(days=i % 6), dtime(18)),
                            ends_at=datetime.combine(WEEK + timedelta(days=i % 6), dtime(19)), place="Ауд. 1",
                            organizer="Студсовет", description="") for i in range(8)])
        dean = User(login="dean", password_hash=hash_password(PASSWORD), role=Role.STAFF, created_at=utcnow())
        s.add_all([dean, User(login="vera", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=1,
                              created_at=utcnow())])
        await s.flush()
        s.add_all([Announcement(source=Source.DEAN, author_user_id=dean.id, title=f"Объявление {i}", body="Текст",
                                audience=Audience.ALL, created_at=utcnow() - timedelta(hours=i)) for i in range(20)])
        await s.commit()
    await engine.dispose()


async def measure(url: str, cache: Cache, repeats: int) -> dict[str, dict]:
    statements: list[str] = []

    def remember(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    settings = Settings(_env_file=None, database_url=url, cookie_secure=False, pyrus_login="", demo_password="")
    app = create_app(settings)
    result: dict[str, dict] = {}
    async with app.router.lifespan_context(app):
        set_cache(cache)  # после старта: lifespan ставит кэш из настроек, подменяем на замеряемый
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://measure") as c:
            r = await c.post("/login", data={"login": "vera", "password": PASSWORD})
            assert r.status_code == 303, r.status_code
            event.listen(Engine, "before_cursor_execute", remember)
            try:
                for title, path in PAGES:
                    assert (await c.get(path)).status_code == 200  # первое открытие (холодный кэш)
                    times, total, schedule = [], 0, 0
                    for _ in range(repeats):
                        statements.clear()
                        started = time.perf_counter()
                        r = await c.get(path)
                        times.append((time.perf_counter() - started) * 1000)
                        assert r.status_code == 200
                        total += len(statements)
                        schedule += sum(1 for s in statements if any(
                            re.search(rf"\b(from|join)\s+\"?{t}\b", s) for t in SCHEDULE_TABLES))
                    result[title] = {"sql": total / repeats, "schedule_sql": schedule / repeats,
                                     "ms_avg": statistics.mean(times), "ms_p50": statistics.median(times)}
            finally:
                event.remove(Engine, "before_cursor_execute", remember)
        await cache.aclose()
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--repeats", type=int, default=50)
    parser.add_argument("--redis", default="", help="CACHE_URL отдельной тестовой базы Redis (не стенда)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite+aiosqlite:///{Path(tmp) / 'measure.db'}"
        await seed(url)
        runs = [("без кэша", Cache(NullBackend())), ("с кэшем (память)", Cache(MemoryBackend()))]
        if args.redis:
            runs.append(("с кэшем (Redis)", Cache(RedisBackend(args.redis))))
        results = [(name, await measure(url, cache, args.repeats)) for name, cache in runs]
    print(f"Повторов на страницу: {args.repeats}; база: sqlite во временной папке; "
          f"занятий в неделе: 30, всего 150; объявлений: 20.\n")
    print(f"| Страница | Режим | SQL на запрос | из них расписание | среднее, мс | медиана, мс |")
    print(f"|---|---|---|---|---|---|")
    for title, _ in PAGES:
        for name, res in results:
            r = res[title]
            print(f"| {title} | {name} | {r['sql']:.1f} | {r['schedule_sql']:.1f} | {r['ms_avg']:.1f} | {r['ms_p50']:.1f} |")


if __name__ == "__main__":
    asyncio.run(main())
