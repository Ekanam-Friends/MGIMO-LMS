"""Успеваемость и кабинет преподавателя: доступ только к своему, баллы доходят до студента,
закрытая ведомость не меняется, итог выставляется по правилам шкалы."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.auth.service import hash_password, utcnow
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.grades import scale
from app.main import create_app
from app.models import (
    Assessment,
    Course,
    CourseEnrollment,
    Discipline,
    GradeEvent,
    Lesson,
    LessonGroup,
    Score,
    Student,
    StudyGroup,
    Teacher,
    lesson_teachers,
)
from app.models.auth import Role, User
from app.models.grades import AssessmentKind, ControlForm, CourseStatus, FinalResult
from app.models.schedule import LessonStatus
from app.web import teacher_views

PASSWORD = "test-only-password"
YEAR = scale.academic_year(date.today())


@pytest.fixture
async def client(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'grades.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    now = datetime(2026, 9, 1)
    past = date.today() - timedelta(days=10)
    async with sf() as s:
        s.add_all([
            StudyGroup(id=10, name="26ТЕСТ-01", number="1ТЕСТ-01", course=1),
            StudyGroup(id=20, name="26ТЕСТ-02", number="1ТЕСТ-02", course=1),
            Teacher(id=1, ruz_oid=1, full_name="Своев Пётр Иванович"),
            Teacher(id=2, ruz_oid=2, full_name="Чужова Анна Сергеевна"),
            Student(id=1, full_name="Тестова Анна Вымышленовна", record_book="Т-1", study_group_id=10),
            Student(id=2, full_name="Примеров Борис Выдуманович", record_book="Т-2", study_group_id=20),
            Discipline(id=1, name="Своя дисциплина"),
            Discipline(id=2, name="Секретная дисциплина Бориса"),
        ])
        await s.flush()
        s.add_all([
            Course(id=1, discipline_id=1, group_id=10, teacher_id=1, semester=1, academic_year=YEAR,
                   control_form=ControlForm.EXAM, created_at=now),
            Course(id=2, discipline_id=2, group_id=20, teacher_id=2, semester=1, academic_year=YEAR,
                   control_form=ControlForm.CREDIT, created_at=now),
        ])
        await s.flush()
        s.add_all([
            CourseEnrollment(course_id=1, student_id=1),
            CourseEnrollment(course_id=2, student_id=2),
            Assessment(id=1, course_id=1, kind=AssessmentKind.TEST, title="Контрольная", max_points=10,
                       due_date=past, position=1, created_at=now),
            Assessment(id=2, course_id=2, kind=AssessmentKind.TEST, title="Чужая контрольная", max_points=10,
                       due_date=past, position=1, created_at=now),
            Assessment(id=3, course_id=1, kind=AssessmentKind.SEMINAR, title="Семинары", max_points=50,
                       due_date=date.today() + timedelta(days=40), position=2, created_at=now),
        ])
        await s.flush()
        s.add_all([
            User(login="teacher", password_hash=hash_password(PASSWORD), role=Role.TEACHER, teacher_id=1, created_at=utcnow()),
            User(login="other", password_hash=hash_password(PASSWORD), role=Role.TEACHER, teacher_id=2, created_at=utcnow()),
            User(login="anna", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=1, created_at=utcnow()),
            User(login="boris", password_hash=hash_password(PASSWORD), role=Role.STUDENT, student_id=2, created_at=utcnow()),
        ])
        await s.commit()

    settings = Settings(database_url=url, cookie_secure=False, pyrus_login="", demo_password="")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.sf = sf  # type: ignore[attr-defined]
            yield c
    await engine.dispose()


async def login(c: httpx.AsyncClient, who: str) -> str:
    c.cookies.clear()
    r = await c.post("/login", data={"login": who, "password": PASSWORD})
    assert r.status_code == 303
    page = await c.get(r.headers["location"])
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


HX = {"HX-Request": "true"}


async def test_teacher_lands_on_own_courses(client):
    r = await client.post("/login", data={"login": "teacher", "password": PASSWORD})
    assert r.headers["location"] == "/t/home"
    page = await client.get("/t/courses")
    assert "Своя дисциплина" in page.text and "Секретная дисциплина Бориса" not in page.text
    assert "концепт для конкурса" in page.text.lower()
    assert (await client.get("/")).headers["location"] == "/t/home"


async def test_teacher_home_is_employee_page(client):
    """Главная преподавателя — «сотрудник» + блок «Преподавателю»; профиль с кадровой карточкой."""
    await login(client, "teacher")
    home = (await client.get("/t/home")).text
    assert "Ресурсы Академии" in home and "Преподавателю" in home and "Мои курсы" in home
    assert "Секретная дисциплина Бориса" not in home
    profile = (await client.get("/profile")).text
    assert "Информация о сотруднике" in profile and "Табельный номер" in profile
    for slug in ("workload", "pps", "kedo", "hr-certificates"):
        assert (await client.get(f"/s/{slug}")).status_code == 200, slug


async def test_foreign_course_is_404(client):
    await login(client, "teacher")
    for path in ["/t/courses/2", "/t/courses/2/final", "/t/courses/2/assessments/2", "/t/courses/2/assessments/new",
                 "/t/courses/1/assessments/2"]:
        assert (await client.get(path)).status_code == 404, path


async def test_student_has_no_teacher_pages(client):
    await login(client, "anna")
    for path in ["/t/home", "/t/services", "/t/courses", "/t/courses/1", "/t/homework", "/t/schedule"]:
        assert (await client.get(path)).status_code == 404, path


async def test_score_reaches_student(client):
    csrf = await login(client, "teacher")
    data = {"assessment_id": "1", "student_id": "1", "points": "8,5"}
    assert (await client.post("/t/courses/1/scores", data=data, headers=HX)).status_code == 403  # без CSRF
    r = await client.post("/t/courses/1/scores", data=data, headers={**HX, "X-CSRF-Token": csrf})
    assert r.status_code == 200 and "score--saved" in r.text and 'id="total-1"' in r.text
    await login(client, "anna")
    page = await client.get("/grades")
    assert "8,5" in page.text and "Своя дисциплина" in page.text
    assert "Секретная дисциплина Бориса" not in page.text
    async with client.sf() as s:  # type: ignore[attr-defined]
        assert await s.scalar(select(func.count()).select_from(GradeEvent).where(GradeEvent.student_id == 1)) == 1


async def test_cannot_grade_outside_own_ledger(client):
    csrf = await login(client, "teacher")
    h = {**HX, "X-CSRF-Token": csrf}
    # студент не из этой ведомости
    r = await client.post("/t/courses/1/scores", data={"assessment_id": "1", "student_id": "2", "points": "5"}, headers=h)
    assert r.status_code == 404
    # задание чужого курса, подставленное в свой
    r = await client.post("/t/courses/1/scores", data={"assessment_id": "2", "student_id": "1", "points": "5"}, headers=h)
    assert r.status_code == 404
    # чужой курс целиком
    r = await client.post("/t/courses/2/scores", data={"assessment_id": "2", "student_id": "2", "points": "5"}, headers=h)
    assert r.status_code == 404
    async with client.sf() as s:  # type: ignore[attr-defined]
        assert await s.scalar(select(func.count()).select_from(Score)) == 0


async def test_points_validated(client):
    csrf = await login(client, "teacher")
    h = {**HX, "X-CSRF-Token": csrf}
    for bad in ["11", "-1", "abc", "nan"]:
        r = await client.post("/t/courses/1/scores", data={"assessment_id": "1", "student_id": "1", "points": bad}, headers=h)
        assert r.status_code == 200 and "score--error" in r.text, bad
    async with client.sf() as s:  # type: ignore[attr-defined]
        assert await s.scalar(select(func.count()).select_from(Score)) == 0


async def test_homework_notifies_only_enrolled(client):
    csrf = await login(client, "teacher")
    r = await client.post("/t/courses/1/assessments", data={
        "csrf": csrf, "kind": "homework", "title": "Эссе", "description": "Написать эссе", "max_points": "10",
        "due_date": (date.today() + timedelta(days=5)).isoformat()})
    assert r.status_code == 303
    r = await client.post("/t/courses/1/assessments", data={
        "csrf": csrf, "kind": "homework", "title": "Без срока", "max_points": "10", "due_date": ""})
    assert r.status_code == 400 and "срок" in r.text
    async with client.sf() as s:  # type: ignore[attr-defined]
        rows = (await s.execute(select(GradeEvent.student_id))).scalars().all()
    assert rows == [1]
    await login(client, "anna")
    page = await client.get("/grades")
    assert "Скоро сдавать" in page.text and "Написать эссе" in page.text


async def test_final_needs_admission_then_locks(client):
    csrf = await login(client, "teacher")
    h = {**HX, "X-CSRF-Token": csrf}
    r = await client.post("/t/courses/1/final", data={"student_id": "1", "result": "5", "exam_points": "40"}, headers=h)
    assert "Нет допуска" in r.text
    await client.post("/t/courses/1/scores", data={"assessment_id": "3", "student_id": "1", "points": "45"}, headers=h)
    r = await client.post("/t/courses/1/final", data={"student_id": "1", "result": "pass"}, headers=h)
    assert "не выставляется" in r.text  # «зачтено» на экзамене нельзя
    r = await client.post("/t/courses/1/final", data={"student_id": "1", "result": "5", "exam_points": "38"}, headers=h)
    assert "final-row--saved" in r.text
    r = await client.post("/t/courses/1/close", data={"csrf": csrf})
    assert r.status_code == 303
    r = await client.post("/t/courses/1/scores", data={"assessment_id": "1", "student_id": "1", "points": "3"}, headers=h)
    assert "Ведомость закрыта" in r.text
    r = await client.post("/t/courses/1/final", data={"student_id": "1", "result": "4", "exam_points": "20"}, headers=h)
    assert "пересдачей" in r.text  # положительный итог после закрытия не переписать
    await login(client, "anna")
    page = await client.get("/grades")
    assert "Отлично" in page.text


async def test_other_teacher_cannot_touch_my_course(client):
    csrf = await login(client, "other")
    h = {**HX, "X-CSRF-Token": csrf}
    r = await client.post("/t/courses/1/scores", data={"assessment_id": "1", "student_id": "1", "points": "1"}, headers=h)
    assert r.status_code == 404
    r = await client.post("/t/courses/1/close", data={"csrf": csrf})
    assert r.status_code == 404
    r = await client.post("/t/courses/1/assessments/1/delete", data={"csrf": csrf})
    assert r.status_code == 404


def test_scale():
    assert scale.suggest(ControlForm.EXAM, 50, 36) == FinalResult.EXCELLENT
    assert scale.suggest(ControlForm.EXAM, 40, 30) == FinalResult.GOOD
    assert scale.suggest(ControlForm.DIFF_CREDIT, 60) == FinalResult.SATISFACTORY
    assert scale.suggest(ControlForm.DIFF_CREDIT, 54.5) == FinalResult.FAIL
    assert scale.suggest(ControlForm.CREDIT, 55) == FinalResult.PASS
    assert scale.suggest(ControlForm.CREDIT, 54) == FinalResult.NO_PASS
    assert scale.semester_number(1, date(2026, 11, 9)) == 1
    assert scale.semester_number(3, date(2027, 3, 1)) == 6
    assert scale.academic_year(date(2027, 1, 20)) == "2026/2027"
    assert scale.fmt_points(7.5) == "7,5" and scale.fmt_points(8.0) == "8"


async def test_teacher_pages_render(client):
    await login(client, "teacher")
    for path in ["/t/courses", "/t/courses/1", "/t/courses/1/final", "/t/courses/1/assessments/1",
                 "/t/courses/1/assessments/new", "/t/homework", "/t/schedule", "/profile"]:
        r = await client.get(path)
        assert r.status_code == 200, path
        assert "концепт для конкурса" in r.text.lower(), path
        assert "Кабинет преподавателя" in r.text or "преподавателя" in r.text, path


# --- исправления после ревью PR #29 ---


async def test_final_error_shown_without_htmx(client):
    csrf = await login(client, "teacher")
    r = await client.post("/t/courses/1/final", data={"csrf": csrf, "student_id": "1", "result": "5", "exam_points": "40"})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    page = await client.get(r.headers["location"])
    assert "Нет допуска" in page.text


async def test_score_comment_kept_by_cell_and_cleared_explicitly(client):
    from app.grades import service as grades
    async with client.sf() as s:  # type: ignore[attr-defined]
        await grades.set_score(s, 1, 1, 1, 1, "5", comment="списано")
        await grades.set_score(s, 1, 1, 1, 1, "6")  # ячейка журнала комментарий не шлёт — он остаётся
        sc = await s.scalar(select(Score).where(Score.assessment_id == 1, Score.student_id == 1))
        assert sc.points == 6 and sc.comment == "списано"
        await grades.set_score(s, 1, 1, 1, 1, "6", comment="")  # явная пустая строка стирает
        assert sc.comment == ""
        await s.commit()


async def test_parallel_insert_of_same_cell_updates_instead_of_500(client):
    from app.grades import service as grades
    async with client.sf() as s:  # type: ignore[attr-defined]
        # «Параллельный» запрос уже вставил балл в эту ячейку, а наш его ещё не видел.
        s.add(Score(assessment_id=1, student_id=1, points=3, comment="", graded_by=1, graded_at=utcnow()))
        await s.flush()
        row = await grades._insert_score(s, 1, 1, 7, None, 1)
        assert row is not None and row.points == 3  # вернулась существующая строка, без IntegrityError
        await s.commit()


async def test_deleted_homework_is_announced(client):
    csrf = await login(client, "teacher")
    r = await client.post("/t/courses/1/assessments", data={
        "csrf": csrf, "kind": "homework", "title": "Лишнее ДЗ", "max_points": "10",
        "due_date": (date.today() + timedelta(days=5)).isoformat()})
    aid = r.headers["location"].split("/assessments/")[1].split("?")[0]
    r = await client.post(f"/t/courses/1/assessments/{aid}/delete", data={"csrf": csrf})
    assert r.status_code == 303
    async with client.sf() as s:  # type: ignore[attr-defined]
        texts = (await s.scalars(select(GradeEvent.text).where(GradeEvent.student_id == 1))).all()
    assert any("«Лишнее ДЗ» отменено" in t for t in texts)


async def test_max_points_parsed_like_points(client):
    csrf = await login(client, "teacher")
    base = {"csrf": csrf, "kind": "test", "title": "Тест", "due_date": ""}
    for bad in ["0", "101", "abc", "nan"]:
        r = await client.post("/t/courses/1/assessments", data={**base, "max_points": bad})
        assert r.status_code == 400 and "Максимум баллов" in r.text, bad
    r = await client.post("/t/courses/1/assessments", data={**base, "max_points": " 7,5 "})
    assert r.status_code == 303


def freeze_teacher_clock(monkeypatch, day, hour=8):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.combine(day, time(hour, 30), tzinfo=tz)

    monkeypatch.setattr(teacher_views, "datetime", FixedDatetime)


async def add_teacher_lessons(client, *entries):
    async with client.sf() as session:
        for lesson_id, (day, hour, status, title) in enumerate(entries, start=900):
            session.add(Lesson(id=lesson_id, date=day, begins_at=time(hour), ends_at=time(hour + 1),
                               discipline=title, status=status, first_seen_at=datetime.now(),
                               last_seen_at=datetime.now(), kind_of_work="семинар"))
            await session.flush()
            session.add(LessonGroup(lesson_id=lesson_id, group_id=10))
            await session.execute(lesson_teachers.insert().values(lesson_id=lesson_id, teacher_id=1))
        await session.commit()


async def test_teacher_home_shows_next_lesson_day(client, monkeypatch):
    """Ссылка на ближайшую пару открывает нужную неделю и день; чужих пар нет."""
    today_date = date.today()
    freeze_teacher_clock(monkeypatch, today_date)
    day = today_date + timedelta(days=8)
    await add_teacher_lessons(client, (day, 10, LessonStatus.ACTIVE, "Своя ближайшая пара"))
    await login(client, "teacher")
    page = (await client.get("/t/home")).text
    assert "Сегодня пар нет" in page and "Ближайшие пары" in page
    href = f"/t/schedule?week={day.isoformat()}#d-{day.isoformat()}"
    assert f'href="{href}"' in page and "Своя ближайшая пара" in page
    assert "Своя ближайшая пара" in (await client.get(href)).text
    await login(client, "other")
    assert "Пар в ближайший месяц нет" in (await client.get("/t/home")).text


@pytest.mark.parametrize("active_today", [True, False])
async def test_cancelled_lessons_do_not_hide_the_next_active_lesson(client, monkeypatch, active_today):
    today_date = date.today()
    freeze_teacher_clock(monkeypatch, today_date)
    active_date = today_date if active_today else today_date + timedelta(days=1)
    await add_teacher_lessons(client, (today_date, 9, LessonStatus.CANCELLED, "Отменённая пара"),
                              (active_date, 10, LessonStatus.ACTIVE, "Действующая пара"))
    await login(client, "teacher")
    page = (await client.get("/t/home")).text
    assert "Отменённая пара" in page and "<small>отменено</small>" in page
    assert "Действующая пара" in page
    if active_today:
        assert "<small>следующая</small>" in page and "· 1 пара</p>" in page
    else:
        assert "· пар нет</p>" in page and "завтра</li>" in page


async def test_teacher_home_uses_moscow_date_and_time(client, monkeypatch):
    server_date = date.today()
    moscow_date = server_date + timedelta(days=1)
    freeze_teacher_clock(monkeypatch, moscow_date, hour=0)
    await add_teacher_lessons(client, (server_date, 10, LessonStatus.ACTIVE, "Вчерашняя пара"),
                              (moscow_date, 10, LessonStatus.ACTIVE, "Сегодняшняя пара"))
    await login(client, "teacher")
    page = (await client.get("/t/home")).text
    assert "Сегодняшняя пара" in page and "Вчерашняя пара" not in page
    assert "<small>следующая</small>" in page


async def test_teacher_home_limits_attention_and_counts_homework_deadlines(client, monkeypatch):
    today_date = date.today()
    freeze_teacher_clock(monkeypatch, today_date)
    async with client.sf() as session:
        course = await session.get(Course, 1)
        session.add(Course(id=3, discipline_id=course.discipline_id, group_id=20, teacher_id=1,
                           semester=1, academic_year=YEAR, control_form=ControlForm.CREDIT,
                           status=CourseStatus.CLOSED, created_at=datetime.now()))
        await session.flush()
        session.add(CourseEnrollment(course_id=3, student_id=2))
        for assessment_id, course_id, offset, kind, title in (
            (900, 1, -8, AssessmentKind.HOMEWORK, "Давняя просрочка"),
            (901, 3, -1, AssessmentKind.HOMEWORK, "ДЗ закрытой ведомости"),
            (902, 1, 2, AssessmentKind.HOMEWORK, "Проверенное ДЗ"),
            (903, 1, 1, AssessmentKind.TEST, "Ближайшая контрольная"),
        ):
            session.add(Assessment(id=assessment_id, course_id=course_id, due_date=today_date + timedelta(days=offset),
                                   kind=kind, title=title, max_points=10, position=4, created_at=datetime.now()))
        await session.flush()
        session.add(Score(assessment_id=902, student_id=1, points=8, graded_by=1, graded_at=datetime.now()))
        await session.commit()
    await login(client, "teacher")
    page = (await client.get("/t/home")).text
    assert "Давняя просрочка" not in page and "ДЗ закрытой ведомости" not in page
    assert "Ближайшая контрольная" not in page
    assert "<b>1</b> срок ДЗ на неделе" in page
    assert "Нет заданий со сроком в пределах недели, требующих проверки" in page
    assert "Давняя просрочка" in (await client.get("/t/homework")).text
