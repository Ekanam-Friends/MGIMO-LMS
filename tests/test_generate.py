"""Генератор контингента: идемпотентность, языковые подгруппы, демо-профили, без будущих дат."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
from sqlalchemy import event, func, select

from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.auth.service import hash_password
from app.generate.run import generate, reset
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
from app.models.notifications import Announcement, Audience, NotificationRead, Source
from app.models.document_notice import DocumentNotice, DocumentRecipient
from app.models.knowledge import KnowledgeDocument

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


@pytest.mark.parametrize("with_reads", [False, True])
async def test_reset_removes_generated_addressees_but_keeps_other_announcements(tmp_path, with_reads):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'reset.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sf = make_session_factory(engine)
        now = datetime(2026, 11, 15)
        async with sf() as db:
            db.add_all([
                Student(id=1, full_name="Примеров Иван Вымышленович", record_book="ГЕН-1"),
                Student(id=2, full_name="Тестова Анна Выдумановна", record_book="ДЕМО-2"),
            ])
            await db.flush()
            password_hash = hash_password("test-only-password")
            db.add_all([
                User(id=1, login="generated", student_id=1, role=Role.STUDENT,
                     password_hash=password_hash, created_at=now),
                User(id=2, login="dean", role=Role.STAFF, password_hash=password_hash, created_at=now),
            ])
            await db.flush()
            db.add_all([
                Announcement(id=1, source=Source.DEAN, author_user_id=2, title="Сгенерированному",
                             audience=Audience.STUDENT, student_id=1, created_at=now),
                Announcement(id=2, source=Source.DEAN, author_user_id=2, title="Демо-студенту",
                             audience=Audience.STUDENT, student_id=2, created_at=now),
                Announcement(id=3, source=Source.DEAN, author_user_id=2, title="Всем",
                             audience=Audience.ALL, created_at=now),
            ])
            db.add(KnowledgeDocument(id=1, uploaded_by_user_id=2, title="Тестовый документ",
                source_url="https://mgimo.ru/test.pdf", source_key="a" * 64,
                revision="2026", filename="test.pdf", sha256="b" * 64, data=b"%PDF-test",
                reviewed_public=True, status="published", uploaded_at=now))
            await db.flush()
            db.add(DocumentNotice(document_id=1, confirmed_by=2, confirmed_at=now))
            await db.flush()
            db.add_all([DocumentRecipient(document_id=1, student_id=sid, telegram_state="skipped")
                        for sid in (1, 2)])
            if with_reads:
                db.add_all([
                    NotificationRead(user_id=1, key="ann:1", read_at=now),
                    NotificationRead(user_id=2, key="ann:3", read_at=now),
                ])
            await db.commit()

        async with sf() as db:
            await reset(db)
            await db.commit()
            # Повторный сброс безопасен; объявления другим адресатам сохраняются.
            await reset(db)
            await db.commit()
        async with sf() as db:
            assert list((await db.scalars(select(Student.id))).all()) == [2]
            assert list((await db.scalars(select(User.id))).all()) == [2]
            assert list((await db.scalars(select(Announcement.id).order_by(Announcement.id))).all()) == [2, 3]
            assert list((await db.scalars(select(DocumentRecipient.student_id))).all()) == [2]
            assert await db.get(KnowledgeDocument, 1) is not None
            reads = (await db.execute(select(NotificationRead.user_id, NotificationRead.key))).all()
            assert reads == ([(2, "ann:3")] if with_reads else [])
    finally:
        await engine.dispose()
