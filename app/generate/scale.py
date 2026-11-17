"""Подготовка и проверка большого синтетического стенда. По умолчанию только чтение.

    python -m app.generate.scale --check
    python -m app.generate.scale --apply --demo-access

Расписание импортируется отдельно через app.schedule.import_ruz --full.
"""
import argparse
import asyncio
import json
import math
from datetime import date, datetime, timedelta
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError

from sqlalchemy import func, select, update

from app.assistant.teaching import course_ids
from app.auth.service import hash_password, utcnow
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.generate.run import GEN_PREFIX, generate
from app.grades.scale import academic_year
from app.models import Course, CourseEnrollment, ImportRun, Lesson, LessonGroup, Score, Student, StudyGroup, Teacher, lesson_teachers
from app.models.auth import Role, User
from app.models.schedule import GroupScheduleCoverage
from app.schedule.importer import semester_bounds


async def inspect(db, *, students=10000, teachers=800, staff=200, today=None, password=None):
    today = today or date.today()
    year = int(academic_year(today)[:4])
    start, finish = date(year, 9, 1), date(year + 1, 9, 1)
    fresh_since = datetime.combine(today - timedelta(days=7), datetime.min.time())
    semester_start, semester_end = semester_bounds(today)
    confirmed = select(GroupScheduleCoverage.group_id).join(ImportRun).where(
        GroupScheduleCoverage.period_start <= semester_start, GroupScheduleCoverage.period_end >= semester_end,
        ImportRun.finished_at >= fresh_since, ImportRun.status.in_(["ok", "partial"]))
    groups = list((await db.scalars(select(StudyGroup).where(StudyGroup.course.between(1, 6),
        StudyGroup.id.in_(confirmed),
        StudyGroup.id.in_(select(LessonGroup.group_id).join(Lesson).where(
            Lesson.status == "active", Lesson.date >= start, Lesson.date < finish,
            Lesson.last_seen_at >= fresh_since))).order_by(StudyGroup.id))).all())
    gids = [g.id for g in groups]
    candidates = list((await db.scalars(select(Teacher.id).join(lesson_teachers,
        lesson_teachers.c.teacher_id == Teacher.id).join(Lesson).join(LessonGroup)
        .where(LessonGroup.group_id.in_(gids), Lesson.status == "active", Lesson.date >= start,
               Lesson.date < finish, Lesson.last_seen_at >= fresh_since).distinct().order_by(Teacher.id))).all())
    student_count = await db.scalar(select(func.count()).select_from(Student))
    linked_students = await db.scalar(select(func.count()).select_from(User).where(User.role == Role.STUDENT,
        User.student_id.is_not(None)))
    staff_count = await db.scalar(select(func.count()).select_from(User).where(User.role == Role.STAFF))
    # Проверяем фактические зачисления каждого аккаунта преподавателя. Наличие
    # преподавателя в справочнике или в расписании ещё не доказывает доступ LLM.
    account_teachers = list((await db.scalars(select(User.teacher_id).where(
        User.role == Role.TEACHER, User.teacher_id.is_not(None)))).all())
    ready_teachers = 0
    for tid in account_teachers:
        has_students = await db.scalar(select(CourseEnrollment.student_id)
            .where(CourseEnrollment.course_id.in_(course_ids(tid, today))).limit(1))
        ready_teachers += tid in candidates and has_students is not None
    unaccounted = await db.scalar(select(func.count()).select_from(Student).where(~Student.id.in_(
        select(User.student_id).where(User.role == Role.STUDENT, User.student_id.is_not(None)))))
    uncovered = await db.scalar(select(func.count()).select_from(Student).where(
        (Student.study_group_id.is_(None)) | (~Student.study_group_id.in_(gids))))
    latest = await db.scalar(select(ImportRun).order_by(ImportRun.id.desc()).limit(1))
    hashes = list((await db.scalars(select(User.password_hash).distinct())).all())
    access_verified = False
    if password and 0 < len(hashes) <= 16:
        # Общий пароль — только для синтетического стенда. Проверка каждого
        # уникального хэша покрывает все аккаунты без 11К повторных argon2 verify.
        def verify_access():
            try:
                return all(PasswordHasher().verify(encoded, password) for encoded in hashes)
            except (VerificationError, InvalidHashError):
                return False
        access_verified = await asyncio.to_thread(verify_access)
    report = {"checked_at": utcnow().isoformat(), "students": student_count,
        "student_accounts": linked_students, "students_without_accounts": unaccounted,
        "students_without_fresh_schedule": uncovered, "eligible_groups": len(groups),
        "imported_teachers": len(candidates), "teacher_accounts": len(account_teachers),
        "teachers_with_students_and_schedule": ready_teachers, "staff_accounts": staff_count,
        "scores": await db.scalar(select(func.count()).select_from(Score)),
        "demo_password_verified_for_all_accounts": access_verified,
        "last_import": ({"id": latest.id, "status": latest.status,
            "finished_at": latest.finished_at.isoformat() if latest.finished_at else None,
            "failed_groups": latest.groups_failed} if latest else None)}
    report["ready"] = bool(student_count >= students and unaccounted == 0 and uncovered == 0
        and ready_teachers >= teachers and staff_count >= staff and access_verified)
    return report, groups, candidates


async def prepare(*, students=10000, teachers=800, staff=200, apply=False, demo_access=False,
                  database_url=None, password=None, today=None):
    if min(students, teachers, staff) < 1:
        raise ValueError("Численность должна быть положительной")
    if database_url is None:
        cfg = get_settings()
        database_url, password = cfg.database_url, cfg.demo_password
    if apply and (not demo_access or not password):
        raise ValueError("Запись требует --demo-access и DEMO_PASSWORD; только вымышленные данные")
    engine = make_engine(database_url)
    sf = make_session_factory(engine)
    try:
        async with sf() as db:
            report, groups, candidates = await inspect(db, students=students, teachers=teachers, staff=staff, today=today, password=password)
        if not apply:
            return report
        if not groups or len(candidates) < teachers:
            raise ValueError(f"Недостаточное покрытие РУЗ: групп {len(groups)}, преподавателей {len(candidates)}/{teachers}. Сначала успешный импорт --full")
        per_group = math.ceil(students / len(groups))
        if per_group > 40:
            raise ValueError("Не хватает групп с расписанием даже при 40 студентах на группу")
        # Транзакции по группам, повторный запуск продолжает, а не сбрасывает данные.
        await generate(students=students, per_group=per_group, seed="mgimo-lms-2026", do_reset=False,
            only=[g.name for g in groups], today=today, database_url=database_url, demo_password=password,
            fill_existing=True, access_password=password)
        encoded = await asyncio.to_thread(hash_password, password)
        async with sf() as db:
            # Меняем пароли только принадлежащих генератору студентов, остальные
            # существующие аккаунты и их индивидуальные пароли сохраняются.
            await db.execute(update(User).where(User.role == Role.STUDENT, User.student_id.in_(
                select(Student.id).where(Student.record_book.like(f"{GEN_PREFIX}%")))).values(password_hash=encoded))
            existing = set((await db.scalars(select(User.teacher_id).where(User.role == Role.TEACHER,
                User.teacher_id.is_not(None)))).all())
            ready = []
            for tid in candidates:
                if await db.scalar(select(CourseEnrollment.student_id).where(
                    CourseEnrollment.course_id.in_(course_ids(tid, today))).limit(1)) is not None:
                    ready.append(tid)
            if len(ready) < teachers:
                raise ValueError(f"После генерации лишь {len(ready)}/{teachers} преподавателей имеют своих студентов. Записи групп сохранены; проверьте соответствие дисциплин РУЗ и ведомостей")
            selected = [tid for tid in ready if tid in existing]
            selected += [tid for tid in ready if tid not in existing][:max(0, teachers - len(selected))]
            for tid in selected:
                if tid not in existing:
                    db.add(User(login=f"demo-teacher-{tid}", role=Role.TEACHER, teacher_id=tid,
                                password_hash=encoded, created_at=utcnow()))
            count_staff = await db.scalar(select(func.count()).select_from(User).where(User.role == Role.STAFF))
            # Собственный префикс и проверка занятых логинов обеспечивают повторяемость.
            used = set((await db.scalars(select(User.login))).all())
            number = 1
            for _ in range(max(0, staff - count_staff)):
                while f"demo-staff-{number}" in used:
                    number += 1
                login = f"demo-staff-{number}"
                used.add(login)
                db.add(User(login=login, role=Role.STAFF, password_hash=encoded, created_at=utcnow()))
            # Повторное применение DEMO_PASSWORD обновляет только собственные demo-* входы.
            await db.flush()
            await db.execute(update(User).where(User.login.like("demo-teacher-%") | User.login.like("demo-staff-%"))
                             .values(password_hash=encoded))
            await db.commit()
        async with sf() as db:
            report, _, _ = await inspect(db, students=students, teachers=teachers, staff=staff, today=today, password=password)
        return report
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Проверка и дополнение большого вымышленного стенда")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="только проверить данные, без записи (по умолчанию)")
    mode.add_argument("--apply", action="store_true", help="дополнить вымышленные данные; сначала бэкап")
    parser.add_argument("--demo-access", action="store_true", help="выдать DEMO_PASSWORD аккаунтам генератора")
    parser.add_argument("--students", type=int, default=10000)
    parser.add_argument("--teachers", type=int, default=800)
    parser.add_argument("--staff", type=int, default=200)
    args = parser.parse_args()
    try:
        report = asyncio.run(prepare(students=args.students, teachers=args.teachers, staff=args.staff,
            apply=args.apply, demo_access=args.demo_access))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ready"] else 1)


if __name__ == "__main__":
    main()
