"""Повторное заполнение демоданных сохраняет доступ, заданный в БД."""
from datetime import date
from secrets import token_urlsafe

from argon2 import PasswordHasher
from sqlalchemy import delete, select

from app import seed as seed_module
from app.auth.service import authenticate, utcnow
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.grades.scale import academic_year
from app.models import Course, Discipline, StudyGroup, Teacher
from app.models.auth import User


async def test_seed_preserves_all_existing_passwords_and_database_controls_login(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'credentials.db'}"
    engine = make_engine(url)
    factory = make_session_factory(engine)
    original_password, changed_environment = token_urlsafe(24), token_urlsafe(24)
    cfg = Settings(database_url=url, demo_password=original_password)
    monkeypatch.setattr(seed_module, "get_settings", lambda: cfg)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(Discipline(id=1, name="Тестовая дисциплина"))
            for number, (_, group_name) in enumerate(seed_module.TEACHERS, 1):
                db.add(StudyGroup(id=number, name=group_name, number=group_name, course=2))
                db.add(Teacher(id=number, ruz_oid=number, full_name=f"Вымышленный преподаватель {number}"))
            await db.flush()
            for number in (1, 2):
                db.add(Course(discipline_id=1, group_id=number, teacher_id=number,
                              semester=3, academic_year=academic_year(date.today()), created_at=utcnow()))
            await db.commit()

        await seed_module.seed()
        async with factory() as db:
            initial = dict((await db.execute(select(User.login, User.password_hash))).all())
        assert set(initial) == {"student1", "student2", "teacher1", "teacher2", "dean"}
        for encoded in initial.values():
            assert encoded != original_password
            assert PasswordHasher().verify(encoded, original_password)

        cfg.demo_password = changed_environment
        await seed_module.seed()
        async with factory() as db:
            current = dict((await db.execute(select(User.login, User.password_hash))).all())
            assert current == initial
            for login in initial:
                assert await authenticate(db, login, original_password) is not None
                assert await authenticate(db, login, changed_environment) is None
            # Наличие демонстрационного логина в seed не заменяет запись в БД.
            await db.execute(delete(User).where(User.login == "dean"))
            assert await authenticate(db, "dean", original_password) is None
            assert await authenticate(db, "dean", changed_environment) is None
    finally:
        await engine.dispose()
