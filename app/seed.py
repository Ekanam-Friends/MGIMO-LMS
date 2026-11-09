"""Тестовые учётки и вымышленные студенты. Идемпотентно: повторный запуск ничего не дублирует.

    python -m app.seed

Пароль всех тестовых учёток — DEMO_PASSWORD из .env (в коде его нет). При повторном запуске
пароль обновляется, если DEMO_PASSWORD сменили.

Группы студентов — реальные группы из импорта РУЗ (чтобы расписание было живым), а ФИО,
зачётки, приказы — вымышленные. Если группы в БД нет (РУЗ не импортирован), студент
создаётся без группы.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, time, timedelta

from argon2 import PasswordHasher
from sqlalchemy import select

from app.auth.service import hash_password, utcnow
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.models import Faculty, Student, StudyGroup
from app.models.auth import Role, User
from app.models.calendar import Activity, ActivitySignup, ElectiveChoice
from app.web import calendar_view as cv

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

# Выбор элективов и подгрупп для демо (решение 0006). Ставится, только если студент ещё ничего
# не выбрал сам; значения, которых нет в расписании его группы, отбрасываются.
CHOICES = {
    "ДЕМО-24-0117": {"subgroups": ["02-фр/англ (3МЖ-02)"],
                     "disciplines": ["Мастер-класс журналиста: аналитическая журналистика", "СМИ и мировые цивилизации"]},
    "ДЕМО-25-0342": {"subgroups": [], "disciplines": ["французский первый", "немецкий третий"]},
}

# Вымышленные внеучебные активности. Еженедельные — на весь осенний семестр.
SEMESTER = (date(2026, 9, 1), date(2027, 1, 31))
WEEKLY = [
    # (название, день недели 0=пн, начало, конец, место, организатор)
    ("Дебатный клуб", 1, time(18, 30), time(20, 0), "Ауд. 1072", "Студенческий дебатный клуб"),
    ("Модель ООН: подготовка делегатов", 3, time(17, 0), time(19, 0), "Ауд. 2018", "Клуб «Модель ООН»"),
    ("Медиацентр: планёрка", 2, time(16, 0), time(17, 0), "Медиацентр, 3 этаж", "Студенческий медиацентр"),
    ("Хор: репетиция", 4, time(18, 0), time(19, 30), "Актовый зал", "Студенческий хор"),
    ("Шахматный клуб", 5, time(12, 0), time(14, 0), "Холл главного корпуса", "Спортклуб"),
]
ONE_OFF = [
    ("Ярмарка стажировок", datetime(2026, 11, 19, 12, 0), datetime(2026, 11, 19, 15, 0), "Холл главного корпуса", "Центр карьеры"),
    ("Дебаты клуба международного права", datetime(2026, 11, 12, 18, 0), datetime(2026, 11, 12, 20, 0), "Актовый зал", "Клуб международного права"),
    ("Встреча с выпускниками-дипломатами", datetime(2026, 11, 10, 17, 0), datetime(2026, 11, 10, 18, 30), "Ауд. 1072", "Ассоциация выпускников"),
    ("Зимняя школа переговоров: презентация", datetime(2026, 11, 24, 18, 0), datetime(2026, 11, 24, 19, 0), "Ауд. 3014", "Центр переговоров"),
    ("Кинопоказ киноклуба", datetime(2026, 11, 6, 19, 0), datetime(2026, 11, 6, 21, 0), "Ауд. 1072", "Киноклуб"),
]
SIGNUPS = {"ДЕМО-24-0117": ["Дебатный клуб", "Ярмарка стажировок", "Кинопоказ киноклуба"],
           "ДЕМО-25-0342": ["Модель ООН: подготовка делегатов"]}


def _activity_specs() -> list[tuple[str, datetime, datetime, str, str]]:
    specs = []
    for title, weekday, begin, end, place, org in WEEKLY:
        d = SEMESTER[0] + timedelta(days=(weekday - SEMESTER[0].weekday()) % 7)
        while d <= SEMESTER[1]:
            specs.append((title, datetime.combine(d, begin), datetime.combine(d, end), place, org))
            d += timedelta(days=7)
    return specs + ONE_OFF


async def _seed_calendar(db) -> None:
    existing = {(a.title, a.starts_at): a for a in (await db.scalars(select(Activity))).all()}
    for title, start, end, place, org in _activity_specs():
        if (title, start) not in existing:
            a = Activity(title=title, starts_at=start, ends_at=end, place=place, organizer=org, description="")
            db.add(a)
            existing[(title, start)] = a
    await db.flush()
    for record_book, choice in CHOICES.items():
        student = await db.scalar(select(Student).where(Student.record_book == record_book))
        if student is None:
            continue
        if await db.scalar(select(ElectiveChoice).where(ElectiveChoice.student_id == student.id)) is None:
            catalog = await cv.elective_catalog(db, student.study_group_id)
            await cv.set_choices(db, student.id, catalog, choice["subgroups"], choice["disciplines"])
        if await db.scalar(select(ActivitySignup).where(ActivitySignup.student_id == student.id)) is None:
            for (title, _start), a in existing.items():
                if title in SIGNUPS.get(record_book, []):
                    db.add(ActivitySignup(student_id=student.id, activity_id=a.id, created_at=utcnow()))


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
        await db.flush()
        await _seed_calendar(db)
        await db.commit()
    await engine.dispose()
    print("seed: готово —", ", ".join([s["login"] for s in STUDENTS] + [STAFF_LOGIN]))


if __name__ == "__main__":
    asyncio.run(seed())
