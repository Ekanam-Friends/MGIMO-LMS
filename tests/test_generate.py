"""Генератор контингента: идемпотентность, языковые подгруппы, демо-профили, без будущих дат."""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import func, select

from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.generate.run import generate
from app.grades import service as grades
from app.models import (
    CourseEnrollment,
    FinalGrade,
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
from app.models.grades import Course

TODAY = date(2026, 11, 9)


async def _db(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'gen.db'}"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = make_session_factory(engine)
    now = datetime(2026, 9, 1)
    async with sf() as s:
        s.add_all([
            StudyGroup(id=10, name="24ТЕСТ-01", number="3ТЕСТ-01", course=3, speciality="Журналистика"),
            Teacher(id=1, ruz_oid=1, full_name="Лекторов Иван Иванович"),
            Teacher(id=2, ruz_oid=2, full_name="Семинарова Анна Петровна"),
            Teacher(id=3, ruz_oid=3, full_name="Языкова Вера Олеговна"),
            Teacher(id=4, ruz_oid=4, full_name="Языков Пётр Олегович"),
            # демо-студент из seed со слабым профилем и долгом
            Student(id=500, full_name="Придумкин Егор Олегович", record_book="ДЕМО-25-0342", study_group_id=10, course=3),
        ])
        lessons = [
            (1, "Правоведение", "Лекция", 1), (2, "Правоведение", "семинар", 2), (3, "Правоведение", "семинар", 2),
            (4, "Правоведение", "Экзамен", 1), (5, "английский первый", "семинар", 3), (6, "испанский первый", "семинар", 4),
            (7, "объединение групп", "", 1),
        ]
        for lid, disc, kind, tid in lessons:
            s.add(Lesson(id=lid, date=date(2026, 10, lid), begins_at=time(9), ends_at=time(10, 30), discipline=disc,
                         kind_of_work=kind, first_seen_at=now, last_seen_at=now))
        await s.flush()
        for lid, _, _, tid in lessons:
            s.add(LessonGroup(lesson_id=lid, group_id=10))
            await s.execute(lesson_teachers.insert().values(lesson_id=lid, teacher_id=tid))
        await s.commit()
    return url, engine, sf


async def test_generate_is_idempotent_and_realistic(tmp_path):
    url, engine, sf = await _db(tmp_path)
    stats = await generate(students=10, per_group=10, seed="t", do_reset=False, only=[], today=TODAY,
                           database_url=url, demo_password="")
    assert stats["students"] == 10 and stats["groups"] == 1
    again = await generate(students=10, per_group=10, seed="t", do_reset=False, only=[], today=TODAY,
                           database_url=url, demo_password="")
    assert again["students"] == 0 and again["groups_skipped"] == 1

    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Student)) == 11
        assert await s.scalar(select(func.count()).select_from(User).where(User.role == Role.STUDENT)) == 10
        # «объединение групп» — служебная запись РУЗ, не дисциплина
        names = (await s.execute(select(Course.id).where(Course.semester == 5))).scalars().all()
        assert len(names) == 3  # Правоведение + две языковые подгруппы
        law = await s.scalar(select(Course).where(Course.semester == 5, Course.teacher_id == 2))
        assert law is not None and law.control_form == "exam"  # экзамен в расписании; ведёт семинарист
        # каждый студент — ровно в одной языковой подгруппе
        lang_ids = (await s.execute(select(Course.id).where(Course.semester == 5, Course.teacher_id.in_([3, 4])))).scalars().all()
        per_student = (await s.execute(
            select(CourseEnrollment.student_id, func.count()).where(CourseEnrollment.course_id.in_(lang_ids))
            .group_by(CourseEnrollment.student_id))).all()
        assert len(per_student) == 11 and all(n == 1 for _, n in per_student)
        # прошлые семестры 1–4 закрыты итогами, баллы и события — не в будущем
        assert await s.scalar(select(func.count()).select_from(FinalGrade)) > 0
        assert await s.scalar(select(func.max(Score.graded_at))) <= datetime.now()
        assert await s.scalar(select(func.max(GradeEvent.created_at))) <= datetime.now()
        book = await grades.record_book(s, 500, TODAY)
        assert len(book.past) == 4 and book.current is not None
        assert len(book.debts) == 1  # демо-профиль student2
    await engine.dispose()
