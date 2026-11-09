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
from app.models import Activity, ActivitySignup, Lesson, LessonGroup, Student, StudyGroup
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
                   discipline="Своя дисциплина", kind_of_work="Лекция", auditorium="ауд. 101",
                   first_seen_at=now, last_seen_at=now),
            Lesson(id=200, date=date(2026, 11, 5), begins_at=time(9), ends_at=time(10, 20),
                   discipline="Чужая дисциплина", kind_of_work="семинар", first_seen_at=now, last_seen_at=now),
            # Занятия по выбору у группы 10: две языковые подгруппы и два параллельных курса.
            Lesson(id=101, date=date(2026, 11, 5), begins_at=time(10, 35), ends_at=time(11, 55),
                   discipline="английский язык", subgroup="01-англ (1ТЕСТ-01)", kind_of_work="семинар",
                   first_seen_at=now, last_seen_at=now),
            Lesson(id=102, date=date(2026, 11, 5), begins_at=time(10, 35), ends_at=time(11, 55),
                   discipline="немецкий язык", subgroup="02-нем (1ТЕСТ-01)", kind_of_work="семинар",
                   first_seen_at=now, last_seen_at=now),
            Lesson(id=103, date=date(2026, 11, 5), begins_at=time(14, 30), ends_at=time(15, 50),
                   discipline="Курс по выбору А", kind_of_work="семинар", first_seen_at=now, last_seen_at=now),
            Lesson(id=104, date=date(2026, 11, 5), begins_at=time(14, 30), ends_at=time(15, 50),
                   discipline="Курс по выбору Б", kind_of_work="семинар", first_seen_at=now, last_seen_at=now),
            LessonGroup(lesson_id=100, group_id=10),
            LessonGroup(lesson_id=200, group_id=20),
            *[LessonGroup(lesson_id=i, group_id=10) for i in (101, 102, 103, 104)],
            Activity(id=1, title="Тестовый клуб", starts_at=datetime(2026, 11, 5, 18), ends_at=datetime(2026, 11, 5, 19),
                     place="Ауд. 1", organizer="Клуб", description=""),
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
    for path in ["/", "/schedule", "/electives", "/profile", "/mfc/certificates", "/grades", "/s/assistant", "/notifications", "/services"]:
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "концепт для конкурса" in r.text.lower(), path
    assert (await client.get("/s/unknown")).status_code == 404
    assert (await client.get("/partials/calendar?week=2026-11-05")).status_code == 200


# --- календарь из трёх слоёв (решение 0006) ---


async def test_electives_hidden_until_chosen(client):
    csrf = await login(client)
    page = (await client.get("/schedule?week=2026-11-05")).text
    assert "Своя дисциплина" in page
    for name in ("английский язык", "немецкий язык", "Курс по выбору А", "Курс по выбору Б"):
        assert name not in page, name

    choices = (await client.get("/electives")).text
    assert "01-англ (1ТЕСТ-01)" in choices and "Курс по выбору Б" in choices
    r = await client.post("/electives", data={"csrf": csrf, "subgroup": ["01-англ (1ТЕСТ-01)", "чужая подгруппа"],
                                              "discipline": ["Курс по выбору Б", "Чужая дисциплина"]})
    assert r.status_code == 303
    page = (await client.get("/schedule?week=2026-11-05")).text
    assert "английский язык" in page and "Курс по выбору Б" in page
    assert "немецкий язык" not in page and "Курс по выбору А" not in page
    assert "Чужая дисциплина" not in page  # выбрать дисциплину чужой группы нельзя


async def test_electives_need_csrf(client):
    await login(client)
    r = await client.post("/electives", data={"subgroup": "01-англ (1ТЕСТ-01)"})
    assert r.status_code == 403


async def test_activity_signup_only_for_self(client, tmp_path):
    csrf = await login(client)
    assert (await client.post("/activities/1/signup", data={"week": "2026-11-05"})).status_code == 403
    assert (await client.post("/activities/999/signup", data={"csrf": csrf})).status_code == 404
    assert (await client.post("/activities/1/delete", data={"csrf": csrf})).status_code == 404

    page = (await client.get("/schedule?week=2026-11-05")).text
    assert "Тестовый клуб" in page and "можно записаться" in page
    r = await client.post("/activities/1/signup", data={"csrf": csrf, "week": "2026-11-05", "size": "full"},
                          headers={"HX-Request": "true"})
    assert r.status_code == 200 and "✓ вы идёте" in r.text and 'id="cal-main"' in r.text

    client.cookies.clear()
    await login(client, "boris")
    page = (await client.get("/schedule?week=2026-11-05")).text
    assert "Тестовый клуб" in page and "✓ вы идёте" not in page  # чужая запись не видна

    client.cookies.clear()
    csrf = await login(client)
    r = await client.post("/activities/1/cancel", data={"csrf": csrf, "week": "2026-11-05", "back": "/schedule"})
    assert r.status_code == 303 and r.headers["location"] == "/schedule?week=2026-11-05"
    assert "✓ вы идёте" not in (await client.get("/schedule?week=2026-11-05")).text


async def test_signup_back_is_not_open_redirect(client):
    csrf = await login(client)
    r = await client.post("/activities/1/signup", data={"csrf": csrf, "back": "https://evil.example"})
    assert r.status_code == 303 and r.headers["location"].startswith("/?d=")


async def test_event_card_shows_room_and_time(client):
    await login(client)
    page = (await client.get("/schedule?week=2026-11-05")).text
    card = page[page.index('class="ev-pop ev-pop--core"'):]
    card = card[:card.index("lesson-card__foot")]
    assert "Своя дисциплина" in card and "09:00 – 10:20" in card and "Аудитория: <b>ауд. 101</b>" in card
    assert 'popovertarget="ev-2026-11-05-0"' in page  # плашка в сетке раскрывает эту карточку
