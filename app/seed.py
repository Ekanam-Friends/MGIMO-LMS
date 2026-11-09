"""Тестовые учётки и вымышленные студенты. Идемпотентно: повторный запуск ничего не дублирует.

    python -m app.seed
    python -m app.generate   # потом: тысячи студентов и успеваемость, учётки teacher1/teacher2

Учётки преподавателей (teacher1, teacher2) привязываются к преподавателю, который ведёт больше
всего ведомостей у группы student1 / student2, — чтобы жюри могло поставить оценку и тут же
увидеть её у студента. Пока ведомостей нет (generate не запускали), их не создаём.

Пароль всех тестовых учёток — DEMO_PASSWORD из .env (в коде его нет). При повторном запуске
пароль обновляется, если DEMO_PASSWORD сменили.

Группы студентов — реальные группы из импорта РУЗ (чтобы расписание было живым), а ФИО,
зачётки, приказы — вымышленные. Если группы в БД нет (РУЗ не импортирован), студент
создаётся без группы.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from argon2 import PasswordHasher
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import hash_password, utcnow
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.grades.scale import academic_year
from app.models import Course, Faculty, Student, StudyGroup, Teacher
from app.models.auth import Role, User

STUDENTS = [
    {
        "login": "student1",
        "group_name": "24МЖ-02",
        "student": dict(
            full_name="Вымыслова Алиса Петровна", gender="f", birth_date=date(2006, 3, 14),
            record_book="ДЕМО-24-0117", course=3, education_basis="бюджетная",
            enrollment_order="№ 2417-с от 18.08.2024", enrollment_date=date(2024, 9, 1),
            expected_graduation=date(2028, 6, 30),
        ),
    },
    {
        "login": "student2",
        "group_name": "25МО-24",
        "student": dict(
            full_name="Придумкин Егор Олегович", gender="m", birth_date=date(2007, 11, 2),
            record_book="ДЕМО-25-0342", course=2, education_basis="договорная",
            enrollment_order="№ 3108-с от 22.08.2025", enrollment_date=date(2025, 9, 1),
            expected_graduation=date(2029, 6, 30),
        ),
    },
]
STAFF_LOGIN = "dean"
TEACHERS = [("teacher1", "24МЖ-02"), ("teacher2", "25МО-24")]
"""(логин, группа): преподаватель берётся из ведомостей этой группы."""


async def _set_password(user: User, password: str) -> None:
    try:
        PasswordHasher().verify(user.password_hash, password)
    except Exception:
        user.password_hash = hash_password(password)


async def seed() -> None:
    cfg = get_settings()
    if not cfg.demo_password:
        sys.exit("Задай DEMO_PASSWORD в .env — пароль тестовых учёток.")
    engine = make_engine(cfg.database_url)
    factory = make_session_factory(engine)
    async with factory() as db:
        for spec in STUDENTS:
            data = dict(spec["student"])
            group = await db.scalar(select(StudyGroup).where(StudyGroup.name == spec["group_name"]))
            if group is not None:
                faculty = await db.get(Faculty, group.faculty_id) if group.faculty_id else None
                data.update(
                    study_group_id=group.id, course=group.course or data["course"],
                    faculty_name=f"Факультет {faculty.name.lower()}" if faculty else "",
                    speciality=group.speciality, form_of_education=(group.form_of_education or "очная").lower(),
                )
            else:
                print(f"группа {spec['group_name']} не найдена — студент без расписания")
            student = await db.scalar(select(Student).where(Student.record_book == data["record_book"]))
            if student is None:
                student = Student(**data)
                db.add(student)
                await db.flush()
            else:
                for k, v in data.items():
                    setattr(student, k, v)

            user = await db.scalar(select(User).where(User.login == spec["login"]))
            if user is None:
                db.add(User(login=spec["login"], password_hash=hash_password(cfg.demo_password),
                            role=Role.STUDENT, student_id=student.id, created_at=utcnow()))
            else:
                user.student_id = student.id
                await _set_password(user, cfg.demo_password)

        staff = await db.scalar(select(User).where(User.login == STAFF_LOGIN))
        if staff is None:
            db.add(User(login=STAFF_LOGIN, password_hash=hash_password(cfg.demo_password),
                        role=Role.STAFF, created_at=utcnow()))
        else:
            await _set_password(staff, cfg.demo_password)
        await db.commit()
        linked = await link_demo_teachers(db, cfg.demo_password)
        await db.commit()
    await engine.dispose()
    print("seed: готово —", ", ".join([s["login"] for s in STUDENTS] + [STAFF_LOGIN] + [lg for lg, _ in linked]))


async def link_demo_teachers(db: AsyncSession, password: str | None = None) -> list[tuple[str, str]]:
    """Создаёт или обновляет учётки teacher1/teacher2. Возвращает [(логин, ФИО)]."""
    if password is None:
        password = get_settings().demo_password
    if not password:
        return []
    year = academic_year(date.today())
    out: list[tuple[str, str]] = []
    used: set[int] = set()
    for login, group_name in TEACHERS:
        user = await db.scalar(select(User).where(User.login == login))
        taken = set((await db.scalars(select(User.teacher_id).where(User.teacher_id.is_not(None), User.login != login))).all())
        if user is not None and user.teacher_id and user.teacher_id not in used:
            teacher_id = user.teacher_id
        else:
            row = (await db.execute(
                select(Course.teacher_id, func.count())
                .join(StudyGroup, StudyGroup.id == Course.group_id)
                .where(StudyGroup.name == group_name, Course.academic_year == year, Course.teacher_id.is_not(None),
                       Course.teacher_id.not_in(taken | used))
                .group_by(Course.teacher_id).order_by(func.count().desc(), Course.teacher_id).limit(1)
            )).first()
            if row is None:
                continue
            teacher_id = row[0]
        used.add(teacher_id)
        if user is None:
            db.add(User(login=login, password_hash=hash_password(password), role=Role.TEACHER,
                        teacher_id=teacher_id, created_at=utcnow()))
        else:
            user.role, user.teacher_id = Role.TEACHER, teacher_id
            await _set_password(user, password)
        teacher = await db.get(Teacher, teacher_id)
        out.append((login, teacher.full_name if teacher else str(teacher_id)))
    return out


if __name__ == "__main__":
    asyncio.run(seed())
