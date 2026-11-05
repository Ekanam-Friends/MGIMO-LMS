"""Telegram-бот: привязка по коду, лента календаря, уведомления — и «студент видит только своё»."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urlparse

import httpx
import pytest

from app.auth.service import hash_password, utcnow
from app.bot import link as links
from app.bot import notify
from app.bot.formatting import format_day
from app.bot.scheduler import digest_due
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.main import create_app
from app.models import (
    CertificateRequestEvent,
    ImportRun,
    Lesson,
    LessonChange,
    LessonGroup,
    Student,
    StudyGroup,
    TelegramLink,
)
from app.models.auth import Role, User
from app.models.certificates import CertificateKind, EventKind, RequestStatus
from app.models.schedule import ChangeKind
from app.certificates import service as certs
from app.web import views
from app.web.schedule_view import lessons_for

PASSWORD = "test-only-password"
DAY = date(2026, 11, 5)
NOW = datetime(2026, 11, 1)


@pytest.fixture
async def db(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    async with sf() as s:
        s.add_all([
            StudyGroup(id=10, name="26ТЕСТ-01", number="1ТЕСТ-01"),
            StudyGroup(id=20, name="26ТЕСТ-02", number="1ТЕСТ-02"),
            Student(id=1, full_name="Тестова Анна Вымышленовна", record_book="Т-1", study_group_id=10),
            Student(id=2, full_name="Примеров Борис Выдуманович", record_book="Т-2", study_group_id=20),
            Lesson(id=100, date=DAY, begins_at=time(9), ends_at=time(10, 20), discipline="Своя дисциплина",
                   kind_of_work="Лекция", auditorium="101", first_seen_at=NOW, last_seen_at=NOW),
            Lesson(id=101, date=DAY + timedelta(days=2), begins_at=time(12), ends_at=time(13, 20),
                   discipline="Своя дисциплина", kind_of_work="Экзамен", auditorium="202",
                   first_seen_at=NOW, last_seen_at=NOW),
            Lesson(id=200, date=DAY, begins_at=time(9), ends_at=time(10, 20), discipline="Чужая дисциплина",
                   kind_of_work="семинар", first_seen_at=NOW, last_seen_at=NOW),
            LessonGroup(lesson_id=100, group_id=10),
            LessonGroup(lesson_id=101, group_id=10),
            LessonGroup(lesson_id=200, group_id=20),
        ])
        await s.flush()
        s.add_all([
            User(login="anna", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=1, created_at=utcnow()),
            User(login="boris", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=2, created_at=utcnow()),
        ])
        await s.commit()
    yield url, sf
    await engine.dispose()


# --- привязка -----------------------------------------------------------------


async def test_code_is_single_use_and_expires(db):
    _, sf = db
    async with sf() as s:
        code = await links.issue_code(s, 1, ttl=timedelta(minutes=15))
        link, student = await links.consume_code(s, code, telegram_id=555)
        assert student.id == 1 and link.telegram_id == 555 and link.feed_token
        with pytest.raises(links.LinkError):
            await links.consume_code(s, code, telegram_id=555)
        expired = await links.issue_code(s, 2, ttl=timedelta(minutes=-1))
        with pytest.raises(links.LinkError):
            await links.consume_code(s, expired, telegram_id=777)
        with pytest.raises(links.LinkError):
            await links.consume_code(s, "выдуманный-код", telegram_id=777)


async def test_relink_moves_telegram_and_new_code_kills_old(db):
    _, sf = db
    async with sf() as s:
        old = await links.issue_code(s, 1, ttl=timedelta(minutes=15))
        fresh = await links.issue_code(s, 1, ttl=timedelta(minutes=15))
        with pytest.raises(links.LinkError):
            await links.consume_code(s, old, telegram_id=555)
        await links.consume_code(s, fresh, telegram_id=555)
        # Тот же Telegram подключает другого студента — привязка переезжает, а не дублируется.
        await links.consume_code(s, await links.issue_code(s, 2, ttl=timedelta(minutes=15)), telegram_id=555)
        me = await links.by_telegram(s, 555)
        assert me is not None and me[1].id == 2
        assert await links.by_student(s, 1) is None


async def test_unlink_removes_everything(db):
    _, sf = db
    async with sf() as s:
        link, _ = await links.consume_code(s, await links.issue_code(s, 1, ttl=timedelta(minutes=15)), telegram_id=555)
        token = link.feed_token
        await notify.mark_once(s, 1, "exam:101")
        assert await links.unlink(s, 1)
        assert await links.by_feed_token(s, token) is None
        assert await notify.mark_once(s, 1, "exam:101")  # журнал напоминаний очищен


# --- веб: профиль и лента -------------------------------------------------------


@pytest.fixture
async def client(db, monkeypatch):
    url, _ = db
    monkeypatch.setattr(views, "today", lambda: DAY)
    settings = Settings(database_url=url, cookie_secure=False, pyrus_login="", demo_password="",
                        telegram_bot_username="Test_bot", public_base_url="https://lms.test")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def _login(c, who="anna") -> str:
    r = await c.post("/login", data={"login": who, "password": PASSWORD})
    assert r.status_code == 303
    page = await c.get("/profile")
    assert "Подключить Telegram" in page.text
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


async def test_profile_link_flow_and_feed(client, db):
    _, sf = db
    csrf = await _login(client)
    assert (await client.post("/profile/telegram/link")).status_code == 403  # без CSRF
    r = await client.post("/profile/telegram/link", data={"csrf": csrf})
    assert r.status_code == 303
    target = urlparse(r.headers["location"])
    assert target.netloc == "t.me" and target.path == "/Test_bot"
    code = target.query.removeprefix("start=")
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,64}", code)

    async with sf() as s:
        link, _ = await links.consume_code(s, code, telegram_id=555)
        await s.commit()
    page = await client.get("/profile")
    assert "Подключён" in page.text and "Отключить" in page.text

    feed = await client.get(f"/feed/{link.feed_token}.ics")
    assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/calendar")
    assert "Своя дисциплина" in feed.text and "Чужая дисциплина" not in feed.text
    assert "DTSTART:20261105T060000Z" in feed.text  # 09:00 МСК в UTC
    assert (await client.get("/feed/не-тот-токен.ics")).status_code == 404

    r = await client.post("/profile/telegram/unlink", data={"csrf": csrf})
    assert r.status_code == 303
    assert (await client.get(f"/feed/{link.feed_token}.ics")).status_code == 404


async def test_services_page_shows_bot_and_own_feed(client, db):
    _, sf = db
    await _login(client)
    page = await client.get("/services")
    assert page.status_code == 200
    assert "Подключить Telegram" in page.text and "появится после подключения" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)

    async with sf() as s:  # anna — студент 1
        link, _ = await links.consume_code(s, await links.issue_code(s, 1, ttl=timedelta(minutes=5)), telegram_id=555)
        other, _ = await links.consume_code(s, await links.issue_code(s, 2, ttl=timedelta(minutes=5)), telegram_id=777)
        await s.commit()
    page = await client.get("/services")
    assert "Подключён" in page.text
    assert f"https://lms.test/feed/{link.feed_token}.ics" in page.text
    assert f"webcal://lms.test/feed/{link.feed_token}.ics" in page.text
    assert other.feed_token not in page.text  # чужая лента не видна

    r = await client.post("/profile/telegram/unlink", data={"csrf": csrf, "back": "/services"})
    assert r.status_code == 303 and r.headers["location"] == "/services"


async def test_unlink_back_is_not_open_redirect(client, db):
    csrf = await _login(client)
    r = await client.post("/profile/telegram/unlink", data={"csrf": csrf, "back": "https://evil.example/"})
    assert r.status_code == 303 and r.headers["location"] == "/profile"


async def test_staff_cannot_issue_code(client, db):
    _, sf = db
    async with sf() as s:
        s.add(User(login="dean", password_hash=hash_password(PASSWORD), role=Role.STAFF, created_at=utcnow()))
        await s.commit()
    await client.post("/login", data={"login": "dean", "password": PASSWORD})
    page = await client.get("/profile")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    assert (await client.post("/profile/telegram/link", data={"csrf": csrf})).status_code == 404


# --- уведомления ------------------------------------------------------------------


async def _link(sf, student_id: int, telegram_id: int) -> None:
    async with sf() as s:
        await links.consume_code(s, await links.issue_code(s, student_id, ttl=timedelta(minutes=5)), telegram_id)
        await s.commit()


async def test_schedule_change_goes_only_to_own_group(db):
    _, sf = db
    await _link(sf, 1, 555)
    await _link(sf, 2, 777)
    async with sf() as s:
        s.add(ImportRun(id=1, started_at=NOW, period_start=DAY, period_end=DAY, status="ok"))
        s.add(LessonChange(run_id=1, lesson_id=100, kind=ChangeKind.CHANGED,
                           details={"changed": {"auditorium": ["101", "505"]}}, detected_at=NOW))
        await s.commit()
    async with sf() as s:
        # Первый проход ставит курсор на конец журнала: старое не рассылаем.
        assert await notify.collect_schedule_changes(s, today=DAY) == {}
        await s.commit()
    async with sf() as s:
        s.add(LessonChange(run_id=1, lesson_id=100, kind=ChangeKind.CHANGED,
                           details={"changed": {"auditorium": ["505", "606"]}}, detected_at=NOW))
        await s.commit()
    async with sf() as s:
        out = await notify.collect_schedule_changes(s, today=DAY)
        await s.commit()
    assert set(out) == {555}
    assert "606" in out[555][0] and "Своя дисциплина" in out[555][0]
    async with sf() as s:
        assert await notify.collect_schedule_changes(s, today=DAY) == {}


async def test_certificate_events_marked_even_without_bot(db):
    _, sf = db
    await _link(sf, 1, 555)
    async with sf() as s:
        mine = await certs.create_request(s, 1, kind=CertificateKind.STUDY, purpose="в банк")
        theirs = await certs.create_request(s, 2, kind=CertificateKind.STUDY, purpose="секрет")
        for req in (mine, theirs):
            s.add(CertificateRequestEvent(request_id=req.id, kind=EventKind.STATUS, status=RequestStatus.READY,
                                          created_at=NOW))
        await s.commit()
    async with sf() as s:
        out = await notify.collect_certificate_events(s)
        await s.commit()
    assert set(out) == {555} and "Готова" in out[555][0]
    async with sf() as s:
        assert await notify.collect_certificate_events(s) == {}


# --- тексты и расписание --------------------------------------------------------------


async def test_day_text_and_digest_rule(db):
    _, sf = db
    async with sf() as s:
        days = await lessons_for(s, 10, DAY, DAY)
    text = format_day(DAY, days[DAY])
    assert "<b>09:00–10:20</b>" in text and "Своя дисциплина (лекция)" in text and "Чужая" not in text

    link = TelegramLink(student_id=1, telegram_id=1, linked_at=NOW, feed_token="x", digest_at="07:30")
    from zoneinfo import ZoneInfo
    msk = ZoneInfo("Europe/Moscow")
    assert not digest_due(link, datetime(2026, 11, 5, 7, 29, tzinfo=msk))
    assert digest_due(link, datetime(2026, 11, 5, 7, 31, tzinfo=msk))
    assert not digest_due(link, datetime(2026, 11, 5, 13, 0, tzinfo=msk))
    link.digest_sent_on = DAY
    assert not digest_due(link, datetime(2026, 11, 5, 8, 0, tzinfo=msk))
