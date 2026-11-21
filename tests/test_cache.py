"""Кэш чтений (решение 0027): повторное чтение без SQL, свежесть после записи, изоляция, fail-open."""

from __future__ import annotations

import contextlib
import re
import time
from datetime import datetime

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.assistant.bootstrap import create_assistant
from app.assistant.conversation import Conversation
from app.auth.service import hash_password, utcnow
from app.cache import Cache, MemoryBackend, RedisBackend, get_cache, set_cache
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.models import Announcement, Student, StudyGroup
from app.models.auth import Role, User
from app.models.notifications import Audience, Source
from tests.fake_ruz import FakeRuz, group, lesson
from tests.test_assistant import FakeProvider, chunk, config, fixed_today, write_corpus  # noqa: F401
from tests.test_importer import MON, TUE, run_import
from tests.test_web import PASSWORD, client, login  # noqa: F401

SCHEDULE_TABLES = ("lessons", "lesson_groups", "lesson_teachers", "activities", "elective_choices", "activity_signups")
"""Таблицы календаря. `teachers` не в списке: его же читает зачётка на главной (это не расписание)."""
FEED_TABLES = ("announcements", "certificate_request_events", "grade_events", "notification_reads")


@contextlib.contextmanager
def sql_log():
    """Все SQL-запросы любого движка процесса за время блока."""
    statements: list[str] = []

    def remember(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(Engine, "before_cursor_execute", remember)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", remember)


def touching(statements: list[str], tables: tuple[str, ...]) -> list[str]:
    """Запросы, которые читают или пишут эти таблицы (FROM/JOIN/INTO/UPDATE)."""
    return [s for s in statements
            if any(re.search(rf"\b(from|join|into|update)\s+\"?{t}\b", s) for t in tables)]


# --- бэкенд в памяти и fail-open ---------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_memory_backend_ttl_and_size_limit():
    clock = Clock()
    backend = MemoryBackend(max_items=2, clock=clock)
    await backend.set("a", "1", ttl=10)
    await backend.set("b", "2", ttl=10)
    assert await backend.mget(["a"]) == ["1"]  # «a» прочитан — теперь он свежее «b»
    await backend.set("c", "3", ttl=10)
    assert await backend.mget(["a", "b", "c"]) == ["1", None, "3"]  # вытеснен давно не читанный
    assert len(backend) == 2
    clock.now += 11
    assert await backend.mget(["a", "c"]) == [None, None]
    assert len(backend) == 0
    assert await backend.set("k", "x", ttl=5, nx=True) and not await backend.set("k", "y", ttl=5, nx=True)


async def test_generation_bump_hides_old_entries():
    cache = Cache(MemoryBackend())
    first = await cache.generations("schedule")
    assert first == await cache.generations("schedule")
    await cache.bump("schedule")
    assert await cache.generations("schedule") != first


async def test_evicted_generation_is_a_new_generation():
    """Вытесненный (LRU) токен не возвращается к старому значению — старые записи не оживают."""
    backend = MemoryBackend(max_items=2)
    cache = Cache(backend)
    (old,) = await cache.generations("g")
    await cache.set_json(f"data:{old}", {"v": 1}, 60)
    await backend.set("filler", "x", 60)  # вытесняет токен поколения
    (new,) = await cache.generations("g")
    assert new != old


class BrokenBackend:
    def __init__(self) -> None:
        self.calls = 0

    async def mget(self, keys):
        self.calls += 1
        raise ConnectionError("redis://:secret@redis:6379 недоступен")

    async def set(self, key, value, ttl, *, nx=False):
        self.calls += 1
        raise ConnectionError("redis недоступен")

    async def aclose(self):
        pass


async def test_broken_backend_fails_open_with_cooldown(caplog):
    clock = Clock()
    backend = BrokenBackend()
    cache = Cache(backend, cooldown=5, clock=clock)
    assert await cache.get_json("x") is None
    assert await cache.generations("schedule") is None
    await cache.bump("schedule")  # не бросает
    assert cache.errors == 1 and backend.calls == 1  # дальше — пауза, бэкенд не дёргаем
    assert "secret" not in caplog.text
    clock.now += 6
    await cache.set_json("x", {"a": 1}, 60)
    assert backend.calls == 2


async def test_site_works_when_cache_is_down(client):
    await login(client)
    broken = Cache(BrokenBackend())
    set_cache(broken)
    r = await client.get("/schedule?week=2026-11-05")
    assert r.status_code == 200 and "Своя дисциплина" in r.text
    r = await client.get("/notifications/badge")
    assert r.status_code == 200
    assert broken.errors >= 1


# --- расписание ---------------------------------------------------------------


async def test_repeat_schedule_does_not_query_schedule_tables(client):
    await login(client)
    pages = ["/schedule?week=2026-11-05", "/?d=2026-11-05", "/partials/calendar?week=2026-11-05&size=compact",
             "/partials/day?d=2026-11-05"]
    with sql_log() as cold:
        first = [await client.get(p) for p in pages]
    assert touching(cold, SCHEDULE_TABLES)  # первое открытие читает БД
    with sql_log() as warm:
        again = [await client.get(p) for p in pages]
    assert [r.text for r in again] == [r.text for r in first]
    assert all("Своя дисциплина" in r.text for r in again)
    assert touching(warm, SCHEDULE_TABLES) == []


async def test_two_students_never_share_schedule_entries(client):
    await login(client, "anna")
    anna = await client.get("/schedule?week=2026-11-05")
    client.cookies.clear()
    await login(client, "boris")
    boris = await client.get("/schedule?week=2026-11-05")
    assert "Своя дисциплина" in anna.text and "Чужая дисциплина" not in anna.text
    assert "Чужая дисциплина" in boris.text and "Своя дисциплина" not in boris.text
    keys = [k for k in get_cache().backend._data if ":cal:" in k]
    assert any(":cal:s1:g10:" in k for k in keys) and any(":cal:s2:g20:" in k for k in keys)


async def test_elective_choice_refreshes_own_calendar(client):
    csrf = await login(client)
    before = await client.get("/schedule?week=2026-11-05")
    assert "английский язык" not in before.text
    r = await client.post("/electives", data={"subgroup": "01-англ (1ТЕСТ-01)", "csrf": csrf})
    assert r.status_code == 303
    after = await client.get("/schedule?week=2026-11-05")
    assert "английский язык" in after.text


@pytest.fixture
async def imported(tmp_path):
    """База после импорта РУЗ (поддельный сервер) + студент группы 1МО-01."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'import.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    ruz = FakeRuz()
    ruz.groups = [group(7001, "1МО-01", "26МО-01")]
    ruz.schedules = {7001: [lesson(1, MON), lesson(4, TUE, discipline="Логика")]}
    await run_import(sf, ruz)
    async with sf() as s:
        group_id = await s.scalar(select(StudyGroup.id).where(StudyGroup.number == "1МО-01"))
        s.add(Student(id=1, full_name="Тестова Анна Вымышленовна", record_book="Т-1", study_group_id=group_id))
        await s.flush()
        s.add(User(login="anna", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=1,
                   created_at=utcnow()))
        await s.commit()
    settings = Settings(_env_file=None, database_url=url, cookie_secure=False, pyrus_login="", demo_password="")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c, sf, ruz
    await engine.dispose()


async def test_schedule_is_fresh_right_after_import(imported):
    c, sf, ruz = imported
    await login(c)
    first = await c.get("/schedule?week=2026-11-02")
    assert "Логика" in first.text
    with sql_log() as cached_run:
        await c.get("/schedule?week=2026-11-02")
    assert touching(cached_run, SCHEDULE_TABLES) == []

    ruz.schedules = {7001: [lesson(1, MON), lesson(4, TUE, discipline="Новая логика")]}
    await run_import(sf, ruz)

    with sql_log() as after_import:
        fresh = await c.get("/schedule?week=2026-11-02")
    assert "Новая логика" in fresh.text
    assert touching(after_import, ("lessons",))  # поколение сменилось — прочитали из БД


# --- лента уведомлений ----------------------------------------------------------


def unread_count(html: str) -> int:
    return int(re.search(r"Непрочитанных: (\d+)", html).group(1))


async def add_announcement(tmp_path, **values) -> int:
    """Объявление пишет другой «процесс» (отдельная сессия) — кэш web узнаёт об этом по поколению."""
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    async with make_session_factory(engine)() as s:
        author = await s.scalar(select(User.id).where(User.login == "dean"))
        if author is None:
            dean = User(login="dean", password_hash=hash_password(PASSWORD), role=Role.STAFF, created_at=utcnow())
            s.add(dean)
            await s.flush()
            author = dean.id
        row = Announcement(source=Source.DEAN, author_user_id=author, body="", created_at=utcnow(), **values)
        s.add(row)
        await s.commit()
        ann_id = row.id
    await engine.dispose()
    return ann_id


async def test_feed_cached_and_invalidated_by_new_announcement_and_read(client, tmp_path):
    csrf = await login(client)
    assert unread_count((await client.get("/notifications/badge")).text) == 0
    with sql_log() as statements:
        await client.get("/notifications/badge")
    assert touching(statements, FEED_TABLES) == []

    ann_id = await add_announcement(tmp_path, title="Деканат: пересдача", audience=Audience.GROUP, group_id=10)

    panel = await client.get("/notifications/panel")
    assert "Деканат: пересдача" in panel.text
    assert unread_count((await client.get("/notifications/badge")).text) == 1

    r = await client.post(f"/notifications/read/ann/{ann_id}", data={"csrf": csrf})
    assert r.status_code == 204
    assert unread_count((await client.get("/notifications/badge")).text) == 0


async def test_feed_is_per_user(client, tmp_path):
    await login(client, "anna")
    assert "Только для Бориса" not in (await client.get("/notifications/panel")).text  # закэшировали ленту Анны
    await add_announcement(tmp_path, title="Только для Бориса", audience=Audience.STUDENT, student_id=2)
    client.cookies.clear()
    await login(client, "boris")
    assert "Только для Бориса" in (await client.get("/notifications/panel")).text
    client.cookies.clear()
    await login(client, "anna")
    assert "Только для Бориса" not in (await client.get("/notifications/panel")).text


# --- ассистент ----------------------------------------------------------------------


def public_answer_replies():
    return ({"scopes": ["public"], "search_query": "академический отпуск"},
            {"status": "answer", "summary_id": "leave-1"}, {"accepted_ids": ["leave-1"], "complete": True})


async def test_assistant_repeats_cached_answer_only_for_same_user(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider(*public_answer_replies(), *public_answer_replies())
    assistant = create_assistant(config(tmp_path), provider)
    question = "Как оформить академический отпуск?"

    first = await assistant.chat(1, 1, question, [], None, Conversation("o", float("inf")), None)
    assert first["status"] == "answer" and len(provider.calls) == 3
    again = await assistant.chat(1, 1, "  как оформить АКАДЕМИЧЕСКИЙ отпуск ", [], None,
                                 Conversation("o", float("inf")), None)
    assert again == first and len(provider.calls) == 3  # из кэша, GigaChat не вызывался

    other = await assistant.chat(2, 2, question, [], None, Conversation("o2", float("inf")), None)
    assert other["status"] == "answer" and len(provider.calls) == 6  # другой пользователь — свой ответ
    keys = [k for k in get_cache().backend._data if ":asst:" in k]
    assert len(keys) == 2 and any(":asst:u1:" in k for k in keys) and any(":asst:u2:" in k for k in keys)
    assert all("отпуск" not in k for k in keys)  # в ключе хэш, а не текст вопроса


async def test_assistant_follow_up_and_failures_are_not_cached(tmp_path):
    write_corpus(tmp_path, [chunk()])
    not_found = ({"scopes": ["public"], "search_query": "академический отпуск"}, {"status": "not_found"})
    provider = FakeProvider(*public_answer_replies(), *public_answer_replies(), *not_found, *not_found)
    assistant = create_assistant(config(tmp_path), provider)
    conversation = Conversation("o", float("inf"))
    await assistant.chat(1, 1, "Академический отпуск", [], None, conversation, None)
    # Тот же вопрос вторым ходом беседы: ответ зависит от истории — без кэша.
    await assistant.chat(1, 1, "Академический отпуск", [], None, conversation, None)
    assert len(provider.calls) == 6
    for _ in range(2):  # «не найдено» не кэшируется
        result = await assistant.chat(1, 1, "Какие условия академического отпуска?", [], None,
                                      Conversation("o", float("inf")), None)
        assert result["status"] == "not_found"
    assert len(provider.calls) == 10


async def test_assistant_cache_follows_corpus_version(tmp_path):
    write_corpus(tmp_path, [chunk()])
    provider = FakeProvider(*public_answer_replies(), *public_answer_replies())
    assistant = create_assistant(config(tmp_path), provider)
    await assistant.chat(1, 1, "Академический отпуск", [], None, Conversation("o", float("inf")), None)
    write_corpus(tmp_path, [chunk(), chunk(id="leave-2", clause="п. 2.2")])  # сотрудник опубликовал документ
    await assistant.chat(1, 1, "Академический отпуск", [], None, Conversation("o", float("inf")), None)
    assert len(provider.calls) == 6


async def test_assistant_cache_disabled_by_zero_ttl(tmp_path):
    write_corpus(tmp_path, [chunk()])
    set_cache(Cache(MemoryBackend(), assistant_ttl=0))
    provider = FakeProvider(*public_answer_replies(), *public_answer_replies())
    assistant = create_assistant(config(tmp_path), provider)
    for _ in range(2):
        await assistant.chat(1, 1, "Академический отпуск", [], None, Conversation("o", float("inf")), None)
    assert len(provider.calls) == 6


def test_datetime_roundtrip_of_feed_items():
    from app.notifications.service import Item, _dump_items, _restore_items

    item = Item("ann", 1, Source.DEAN, "t", "b", datetime(2026, 11, 21, 10, 30), "/x", False)
    assert _restore_items(_dump_items([item])) == [item]


async def test_unreachable_redis_is_fail_open():
    """Настоящий клиент redis.asyncio на закрытом порту: ошибка поймана, ответ — «нет в кэше»."""
    cache = Cache(RedisBackend("redis://127.0.0.1:1/0", timeout=0.2))
    started = time.monotonic()
    assert await cache.generations("schedule") is None
    assert await cache.get_json("x") is None  # в паузе после ошибки — без нового подключения
    await cache.set_json("x", {"a": 1}, 60)
    await cache.bump("schedule")
    assert cache.errors == 1 and time.monotonic() - started < 2
    await cache.aclose()
