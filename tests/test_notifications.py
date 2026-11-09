"""Колокольчик и объявления (решение 0008): адресация, права, прочтение, чужое не видно."""

from __future__ import annotations

import re
from datetime import datetime

import httpx
import pytest

from app.auth.service import hash_password, utcnow
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.models import Course, Discipline, GradeEvent, Student, StudyGroup, Teacher
from app.models.auth import Role, User

PASSWORD = "test-only-password"


@pytest.fixture
async def client(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'notif.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    async with sf() as s:
        s.add_all([
            StudyGroup(id=10, name="26ТЕСТ-01", number="1ТЕСТ-01"),
            StudyGroup(id=20, name="26ТЕСТ-02", number="1ТЕСТ-02"),
            Teacher(id=1, ruz_oid=1, full_name="Преподавателев Пётр Вымышленович"),
            Teacher(id=2, ruz_oid=2, full_name="Другов Дмитрий Выдуманович"),
            Discipline(id=1, name="Тестовая дисциплина"),
            Student(id=1, full_name="Тестова Анна Вымышленовна", record_book="Т-1", study_group_id=10, faculty_name="Фак А"),
            Student(id=2, full_name="Примеров Борис Выдуманович", record_book="Т-2", study_group_id=20, faculty_name="Фак Б"),
        ])
        await s.flush()
        s.add_all([
            Course(id=1, discipline_id=1, group_id=10, teacher_id=1, semester=1, academic_year="2026/2027", created_at=utcnow()),
            GradeEvent(student_id=1, course_id=1, kind="score", text="Контрольная: 8 из 10", created_at=utcnow()),
            GradeEvent(student_id=2, course_id=1, kind="score", text="СЕКРЕТ БОРИСА", created_at=utcnow()),
            User(login="anna", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=1, created_at=utcnow()),
            User(login="boris", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=2, created_at=utcnow()),
            User(login="teacher", password_hash=hash_password(PASSWORD), role=Role.TEACHER, teacher_id=1, created_at=utcnow()),
            User(login="dean", password_hash=hash_password(PASSWORD), role=Role.STAFF, created_at=utcnow()),
        ])
        await s.commit()
    await engine.dispose()
    app = create_app(Settings(database_url=url, cookie_secure=False, pyrus_login="", demo_password=""))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def login(c: httpx.AsyncClient, who: str) -> str:
    r = await c.post("/login", data={"login": who, "password": PASSWORD})
    assert r.status_code == 303
    home = await c.get(r.headers["location"], follow_redirects=True)
    return re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)


async def send_staff(c, csrf, **data):
    return await c.post("/d/announcements", data={"csrf": csrf, "title": "Заголовок", "body": "Текст", **data})


async def test_dean_announcement_reaches_only_addressees(client):
    csrf = await login(client, "dean")
    r = await send_staff(client, csrf, title="Для группы 1", audience="group", group="1ТЕСТ-01")
    assert r.status_code == 303
    await send_staff(client, csrf, title="Для факультета Б", audience="faculty", faculty="Фак Б")
    await send_staff(client, csrf, title="Всем от вуза", source="university", audience="all")
    await client.post("/logout", data={"csrf": csrf})

    await login(client, "anna")
    panel = (await client.get("/notifications/panel")).text
    assert "Для группы 1" in panel and "Всем от вуза" in panel and "Для факультета Б" not in panel
    assert "Деканат" in panel and "Вуз" in panel
    await client.post("/logout", data={"csrf": await _csrf(client)})

    await login(client, "boris")
    panel = (await client.get("/notifications/panel")).text
    assert "Для факультета Б" in panel and "Для группы 1" not in panel


async def _csrf(c):
    page = await c.get("/notifications")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1) if 'name="csrf"' in page.text else \
        re.search(r"X-CSRF-Token\": \"([^\"]+)\"", page.text).group(1)


async def test_unknown_group_and_empty_title_rejected(client):
    csrf = await login(client, "dean")
    r = await send_staff(client, csrf, audience="group", group="НЕТ-99")
    assert r.status_code == 400 and "Такой группы нет" in r.text
    r = await send_staff(client, csrf, title="  ")
    assert r.status_code == 400
    r = await send_staff(client, csrf, source="teacher")
    assert r.status_code == 400


async def test_student_cannot_publish_or_open_dean_cabinet(client):
    csrf = await login(client, "anna")
    assert (await client.get("/d/announcements")).status_code == 404
    assert (await send_staff(client, csrf)).status_code == 404
    assert (await client.get("/t/announcements")).status_code == 404


async def test_posts_need_csrf(client):
    await login(client, "dean")
    r = await client.post("/d/announcements", data={"title": "x", "audience": "all"})
    assert r.status_code == 403
    r = await client.post("/notifications/read-all")
    assert r.status_code == 403


async def test_teacher_writes_only_to_own_groups(client):
    csrf = await login(client, "teacher")
    r = await client.post("/t/announcements", data={"csrf": csrf, "title": "Перенос консультации", "body": "в 15:00", "group_id": 20})
    assert r.status_code == 400  # группа 20 не его
    r = await client.post("/t/announcements", data={"csrf": csrf, "title": "Перенос консультации", "body": "в 15:00", "group_id": 10})
    assert r.status_code == 303
    await client.post("/logout", data={"csrf": csrf})

    await login(client, "anna")
    panel = (await client.get("/notifications/panel")).text
    assert "Перенос консультации" in panel and "Преподаватель" in panel
    await client.post("/logout", data={"csrf": await _csrf(client)})
    await login(client, "boris")
    assert "Перенос консультации" not in (await client.get("/notifications/panel")).text


async def test_grade_events_are_own_only(client):
    await login(client, "anna")
    panel = (await client.get("/notifications/panel")).text
    assert "Контрольная: 8 из 10" in panel and "СЕКРЕТ БОРИСА" not in panel


async def test_read_state_and_badge(client):
    csrf = await login(client, "anna")
    assert ">1<" in (await client.get("/notifications/badge")).text
    r = await client.post("/notifications/read/grade/1", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 204 and r.headers["HX-Trigger"] == "notif-changed"
    badge = (await client.get("/notifications/badge")).text
    assert "hidden" in badge and ">0<" in badge
    # чужое событие помечается только в своей записи прочтения и счётчик Бориса не трогает
    await client.post("/notifications/read/grade/2", headers={"X-CSRF-Token": csrf})
    assert (await client.post("/notifications/read/zzz/1", headers={"X-CSRF-Token": csrf})).status_code == 404


async def test_read_all(client):
    csrf = await login(client, "anna")
    r = await client.post("/notifications/read-all", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and "notif--unread" not in r.text
    assert ">0<" in (await client.get("/notifications/badge")).text


async def test_dean_has_no_bell_but_own_cabinet(client):
    await login(client, "dean")
    page = (await client.get("/d/announcements")).text
    assert "Новое объявление" in page and 'id="bell"' not in page
    assert (await client.get("/", follow_redirects=False)).headers["location"] == "/d/announcements"
    assert (await client.get("/profile")).status_code == 200
