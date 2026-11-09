"""Экраны кабинета: вход, «студент видит только своё», CSRF, расписание по группе из сессии."""

from __future__ import annotations

import re
from datetime import date, datetime, time

import httpx
import pytest

from app.auth.service import hash_password, utcnow
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.models import Lesson, LessonGroup, Student, StudyGroup
from app.models.auth import Role, User
from app.models.certificates import CertificateKind
from app.certificates import service as certs

PASSWORD = "test-only-password"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'web.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    now = datetime(2026, 11, 1)
    async with sf() as s:
        s.add_all([
            StudyGroup(id=10, name="26ТЕСТ-01", number="1ТЕСТ-01"),
            StudyGroup(id=20, name="26ТЕСТ-02", number="1ТЕСТ-02"),
            Student(id=1, full_name="Тестова Анна Вымышленовна", record_book="Т-1", study_group_id=10),
            Student(id=2, full_name="Примеров Борис Выдуманович", record_book="Т-2", study_group_id=20),
            Lesson(id=100, date=date(2026, 11, 5), begins_at=time(9), ends_at=time(10, 20),
                   discipline="Своя дисциплина", kind_of_work="Лекция", first_seen_at=now, last_seen_at=now),
            Lesson(id=200, date=date(2026, 11, 5), begins_at=time(9), ends_at=time(10, 20),
                   discipline="Чужая дисциплина", kind_of_work="семинар", first_seen_at=now, last_seen_at=now),
            LessonGroup(lesson_id=100, group_id=10),
            LessonGroup(lesson_id=200, group_id=20),
        ])
        await s.flush()
        s.add_all([
            User(login="anna", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=1, created_at=utcnow()),
            User(login="boris", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=2, created_at=utcnow()),
        ])
        borisreq = await certs.create_request(s, 2, kind=CertificateKind.STUDY, purpose="секрет Бориса")
        await s.commit()
    await engine.dispose()

    settings = Settings(database_url=url, cookie_secure=False, pyrus_login="", demo_password="")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.boris_request_id = borisreq.id  # type: ignore[attr-defined]
            yield c


async def login(c: httpx.AsyncClient, who: str = "anna") -> str:
    r = await c.post("/login", data={"login": who, "password": PASSWORD})
    assert r.status_code == 303
    page = await c.get("/mfc/certificates")
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


async def test_anonymous_redirected_to_login(client):
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    r = await client.get("/partials/day", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


async def test_wrong_password_rejected(client):
    r = await client.post("/login", data={"login": "anna", "password": "nope"})
    assert r.status_code == 400
    assert "lms_session" not in r.cookies
    r = await client.post("/login", data={"login": "nobody", "password": "nope"})
    assert r.status_code == 400


async def test_cookie_flags(client):
    r = await client.post("/login", data={"login": "anna", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


async def test_schedule_only_own_group(client):
    await login(client)
    r = await client.get("/?d=2026-11-05")
    assert "Своя дисциплина" in r.text and "Чужая дисциплина" not in r.text
    r = await client.get("/schedule?week=2026-11-05")
    assert "Своя дисциплина" in r.text and "Чужая дисциплина" not in r.text


async def test_foreign_certificate_is_404(client):
    await login(client)
    rid = client.boris_request_id
    assert (await client.get(f"/mfc/certificates/{rid}")).status_code == 404
    assert (await client.get(f"/mfc/certificates/{rid}/pdf")).status_code == 404
    listing = await client.get("/mfc/certificates")
    assert "секрет Бориса" not in listing.text


async def test_order_certificate_needs_csrf(client):
    csrf = await login(client)
    r = await client.post("/mfc/certificates", data={"kind": "study", "purpose": "в банк", "copies": "1"})
    assert r.status_code == 403
    r = await client.post("/mfc/certificates", data={"kind": "study", "purpose": "в банк", "copies": "1", "csrf": csrf})
    assert r.status_code == 303
    listing = await client.get("/mfc/certificates")
    assert "в банк" in listing.text


async def test_logout_kills_session(client):
    csrf = await login(client)
    r = await client.post("/logout", data={"csrf": csrf})
    assert r.status_code == 303
    client.cookies.clear()
    assert (await client.get("/")).status_code == 303


async def test_all_pages_render(client):
    await login(client)
    for path in ["/", "/schedule", "/profile", "/mfc/certificates", "/grades", "/s/assistant", "/s/notifications", "/services"]:
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "концепт для конкурса" in r.text.lower(), path
    assert (await client.get("/s/unknown")).status_code == 404
