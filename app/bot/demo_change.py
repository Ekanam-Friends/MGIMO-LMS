"""Демонстрация пуша «изменилось расписание» для жюри — без ожидания реального переноса в РУЗ.

    python -m app.bot.demo_change --login student1             # сменить аудиторию ближайшего занятия
    python -m app.bot.demo_change --login student1 --cancel    # отменить ближайшее занятие

Скрипт меняет занятие в БД и пишет запись в журнал `lesson_changes` так же, как импорт РУЗ.
Бот подхватит её в течение минуты. Следующий импорт РУЗ вернёт настоящие значения и сам
пришлёт «изменение обратно» — данные не портятся надолго.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.bot.link import utcnow
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.models import ImportRun, Lesson, LessonChange, LessonGroup, Student
from app.models.auth import User
from app.models.schedule import ChangeKind, LessonStatus

DEMO_STATUS = "demo"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login", required=True, help="логин тестового студента (student1, student2)")
    parser.add_argument("--cancel", action="store_true", help="отменить занятие вместо смены аудитории")
    args = parser.parse_args(argv)

    engine = make_engine(get_settings().database_url)
    sf = make_session_factory(engine)
    try:
        async with sf() as session:
            student = await session.scalar(select(Student).join(User, User.student_id == Student.id).where(User.login == args.login))
            if student is None or student.study_group_id is None:
                print(f"Студент {args.login} не найден или без группы")
                return 1
            now = datetime.now()
            lesson = await session.scalar(
                select(Lesson).join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
                .where(LessonGroup.group_id == student.study_group_id, Lesson.status == LessonStatus.ACTIVE,
                       Lesson.date >= now.date(), Lesson.date <= now.date() + timedelta(days=14))
                .order_by(Lesson.date, Lesson.begins_at).limit(1)
            )
            if lesson is None:
                print("В ближайшие две недели у группы нет занятий")
                return 1
            run = ImportRun(started_at=utcnow(), finished_at=utcnow(), period_start=lesson.date,
                            period_end=lesson.date, status=DEMO_STATUS)
            session.add(run)
            await session.flush()
            if args.cancel:
                lesson.status = LessonStatus.CANCELLED
                snapshot = {"date": lesson.date.isoformat(), "begins_at": lesson.begins_at.isoformat(),
                            "discipline": lesson.discipline, "auditorium": lesson.auditorium}
                session.add(LessonChange(run_id=run.id, lesson_id=lesson.id, kind=ChangeKind.CANCELLED,
                                         details={"lesson": snapshot}, detected_at=utcnow()))
            else:
                old = lesson.auditorium
                lesson.auditorium = "Демо-аудитория 404"
                session.add(LessonChange(run_id=run.id, lesson_id=lesson.id, kind=ChangeKind.CHANGED,
                                         details={"changed": {"auditorium": [old, lesson.auditorium]}},
                                         detected_at=utcnow()))
            await session.commit()
            print(f"Готово: {lesson.discipline}, {lesson.date} {lesson.begins_at:%H:%M} — "
                  f"{'отменено' if args.cancel else 'аудитория изменена'}. Бот пришлёт сообщение в течение минуты.")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
