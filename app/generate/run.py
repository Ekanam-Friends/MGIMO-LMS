"""Генерация вымышленного контингента и успеваемости.

    python -m app.seed                      # сначала тестовые учётки (student1, student2, dean)
    python -m app.generate                  # ~3000 студентов в группах с расписанием
    python -m app.generate --students 6000 --per-group 15
    python -m app.generate --reset          # стереть всё сгенерированное и успеваемость, создать заново

Что создаётся (решение docs/decisions/0007-grades.md):

- студенты в реальных группах из импорта РУЗ (чтобы у каждого было живое расписание), с
  вымышленными ФИО, зачётками «ГЕН-…», приказами; учётка `users` с роль student и случайным
  паролем, который нигде не сохраняется (войти в такие учётки нельзя — для демо есть student1/2);
- ведомости текущего семестра из расписания группы: дисциплина → курс, ведущий преподаватель —
  тот, кто чаще всех ведёт её у группы; языки — по подгруппам;
- контрольные точки (ДЗ, контрольные, работа на семинарах) и баллы за прошедшие;
- прошлые семестры по типовому плану (app/generate/curriculum.py) с итогами, пересдачами и
  редкими долгами;
- последние события в ленте успеваемости.

Идемпотентно: группа, где уже есть сгенерированные студенты, пропускается (в её открытые
ведомости только дописываются новые студенты группы, например student1 после seed).
Детерминированно: то же зерно (`--seed`) — те же люди и оценки.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import re
import secrets
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import hash_password, utcnow
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.generate import curriculum, people
from app.grades import scale
from app.models import (
    Assessment,
    Course,
    CourseEnrollment,
    Discipline,
    Faculty,
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
from app.models.auth import AuthSession, Role, User
from app.models.grades import AssessmentKind, ControlForm, CourseStatus, FinalResult, GradeEventKind
from app.models.notifications import Announcement, NotificationRead

GEN_PREFIX = "ГЕН-"
LANG_RE = re.compile(r"^[а-яё]+(ский|цкий)\s+(первый|второй|третий)$", re.IGNORECASE)
JUNK_RE = re.compile(r"объ?е?динени|объдинени", re.IGNORECASE)

# Демо-учётки из app/seed.py: у student1 — сильная зачётка без долгов, у student2 — средняя
# и один долг, чтобы жюри видело оба состояния экрана.
DEMO_PROFILES = {"ДЕМО-24-0117": (0.88, False), "ДЕМО-25-0342": (0.6, True)}


def _hash_random(_: int) -> str:
    return hash_password(secrets.token_urlsafe(18))


def make_password_hashes(n: int) -> list[str]:
    """argon2 медленный нарочно; тысячи хэшей считаем в нескольких процессах."""
    if n <= 0:
        return []
    if n < 50:
        return [_hash_random(i) for i in range(n)]
    with ProcessPoolExecutor(max_workers=2) as pool:
        return list(pool.map(_hash_random, range(n), chunksize=25))


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _half(x: float) -> float:
    return round(x * 2) / 2


def intake_year(group: StudyGroup, today: date) -> int:
    m = re.match(r"^(\d{2})", group.name or "")
    if m:
        return 2000 + int(m.group(1))
    start = int(scale.academic_year(today)[:4])
    return start - ((group.course or 1) - 1)


def semester_start(year_start: int, semester: int) -> date:
    return date(year_start, 9, 1) if semester % 2 else date(year_start + 1, 2, 9)


@dataclass
class Ctx:
    db: AsyncSession
    today: date
    seed: str
    disciplines: dict[str, int]
    teacher_pool: list[int]
    stats: Counter
    now: datetime


async def _discipline_id(ctx: Ctx, name: str) -> int:
    if name in ctx.disciplines:
        return ctx.disciplines[name]
    d = Discipline(name=name[:500])
    ctx.db.add(d)
    await ctx.db.flush()
    ctx.disciplines[name] = d.id
    return d.id


# --- выбор групп ---


async def pick_groups(db: AsyncSession, students: int, per_group: int, only: list[str]) -> list[StudyGroup]:
    if only:
        rows = await db.scalars(select(StudyGroup).where((StudyGroup.name.in_(only)) | (StudyGroup.number.in_(only))))
        return list(rows.all())
    need = math.ceil(students / per_group)
    demo_groups = set((await db.scalars(
        select(Student.study_group_id).where(Student.study_group_id.is_not(None), ~Student.record_book.like(f"{GEN_PREFIX}%"))
    )).all())
    with_lessons = select(LessonGroup.group_id).distinct()
    rows = list((await db.scalars(
        select(StudyGroup).where(StudyGroup.id.in_(with_lessons), StudyGroup.course.is_not(None),
                                 StudyGroup.course <= 6)
        .order_by(StudyGroup.id)
    )).all())
    first = [g for g in rows if g.id in demo_groups]
    rest = [g for g in rows if g.id not in demo_groups]
    random.Random("groups").shuffle(rest)  # разные факультеты и курсы, а не первые N по id
    picked = (first + rest)[:max(need, len(first))]
    if len(picked) < need:
        print(f"групп с расписанием только {len(picked)} — студентов будет меньше {students}; "
              f"импортируйте больше групп (python -m app.schedule.import_ruz --full) или поднимите --per-group")
    return picked


# --- студенты ---


async def create_students(ctx: Ctx, g: StudyGroup, n: int, hashes: list[str], faculty: Faculty | None,
                          start: int = 1) -> list[int]:
    rng = random.Random(f"{ctx.seed}:students:{g.id}")
    intake = intake_year(g, ctx.today)
    rows = []
    for i in range(start, start + n):
        name, gender = people.student_name(rng)
        rows.append(dict(
            full_name=name, gender=gender,
            birth_date=date(intake - 18, 1, 1) + timedelta(days=rng.randint(0, 364)),
            record_book=f"{GEN_PREFIX}{intake % 100:02d}-{g.id}-{i:02d}",
            study_group_id=g.id,
            faculty_name=f"Факультет {faculty.name.lower()}" if faculty else "",
            course=g.course or 1, level="бакалавриат",
            form_of_education=(g.form_of_education or "очная").lower(),
            education_basis="бюджетная" if rng.random() < 0.62 else "договорная",
            speciality=g.speciality or "",
            enrollment_order=f"№ {rng.randint(1000, 3999)}-с от {date(intake, 8, rng.randint(15, 28)):%d.%m.%Y}",
            enrollment_date=date(intake, 9, 1), expected_graduation=date(intake + 4, 6, 30),
        ))
    ids = list((await ctx.db.scalars(insert(Student).returning(Student.id, sort_by_parameter_order=True), rows)).all())
    await ctx.db.execute(insert(User), [
        dict(login=f"st{g.id}-{i:02d}", password_hash=hashes.pop(), role=Role.STUDENT, student_id=sid, created_at=ctx.now)
        for i, sid in enumerate(ids, start=start)
    ])
    ctx.stats["students"] += len(ids)
    return ids


def abilities(ctx: Ctx, students: list[Student]) -> dict[int, tuple[float, bool]]:
    out = {}
    for s in students:
        if s.record_book in DEMO_PROFILES:
            out[s.id] = DEMO_PROFILES[s.record_book]
        else:
            rng = random.Random(f"{ctx.seed}:ability:{s.id}")
            out[s.id] = (_clip(rng.gauss(0.72, 0.14), 0.3, 0.98), False)
    return out


# --- текущий семестр ---


@dataclass
class CurrentDiscipline:
    name: str
    lessons: int
    has_exam: bool
    teacher_id: int | None


async def group_disciplines(ctx: Ctx, g: StudyGroup) -> list[CurrentDiscipline]:
    rows = (await ctx.db.execute(
        select(Lesson.id, Lesson.discipline, Lesson.kind_of_work, lesson_teachers.c.teacher_id)
        .join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
        .outerjoin(lesson_teachers, lesson_teachers.c.lesson_id == Lesson.id)
        .where(LessonGroup.group_id == g.id)
    )).all()
    lessons: dict[str, set[int]] = defaultdict(set)
    exam: set[str] = set()
    teachers: dict[str, Counter] = defaultdict(Counter)
    for lid, disc, kind, tid in rows:
        disc = (disc or "").strip()
        if not disc or JUNK_RE.search(disc):
            continue
        lessons[disc].add(lid)
        k = (kind or "").lower()
        if k.startswith("экзам"):
            exam.add(disc)
        elif tid is not None:
            teachers[disc][tid] += 2 if k.startswith("семин") else 1  # баллы ставит семинарист
    return [CurrentDiscipline(d, len(ids), d in exam, teachers[d].most_common(1)[0][0] if teachers[d] else None)
            for d, ids in sorted(lessons.items())]


def _plan(form: str, language: bool, year_start: int, semester: int, rng: random.Random) -> list[tuple]:
    """Контрольные точки семестра: (вид, название, описание, максимум, срок)."""
    start = semester_start(year_start, semester)

    def at(days: int) -> date:
        return start + timedelta(days=days + rng.randint(-3, 3))

    hw = rng.sample(curriculum.HOMEWORK, 2)
    if language:
        return [
            (AssessmentKind.SEMINAR, "Работа на занятиях", "", 30, at(112)),
            (AssessmentKind.HOMEWORK, "Письменное задание: " + rng.choice(("эссе-рассуждение", "перевод статьи", "резюме текста")),
             "Объём — 250–300 слов. Сдать в печатном виде на занятии или файлом в СДО.", 15, at(24)),
            (AssessmentKind.TEST, "Лексико-грамматический тест 1", "", 20, at(45)),
            (AssessmentKind.HOMEWORK, "Аудирование и пересказ", "Прослушайте подкаст из списка и подготовьте пересказ на 2–3 минуты.", 15, at(62)),
            (AssessmentKind.TEST, "Лексико-грамматический тест 2", "", 20, at(100)),
        ]
    if form == ControlForm.EXAM:
        weights = (20, 10, 15, 10, 5)
    else:
        weights = (30, 15, 20, 15, 20)
    plan = [
        (AssessmentKind.SEMINAR, "Работа на семинарах", "Выступления, участие в обсуждении, подготовка к занятиям.", weights[0], at(112)),
        (AssessmentKind.HOMEWORK, hw[0][0], hw[0][1], weights[1], at(21)),
        (AssessmentKind.TEST, rng.choice(curriculum.TESTS), "", weights[2], at(42)),
        (AssessmentKind.HOMEWORK, hw[1][0], hw[1][1], weights[3], at(63)),
        (AssessmentKind.ESSAY, rng.choice(curriculum.ESSAYS), "Тема — на выбор из списка на странице курса в СДО.", weights[4], at(98)),
    ]
    return plan


async def current_courses(ctx: Ctx, g: StudyGroup, students: list[Student], ab: dict[int, tuple[float, bool]]) -> None:
    semester = scale.semester_number(g.course or 1, ctx.today)
    year = scale.academic_year(ctx.today)
    year_start = int(year[:4])
    found = await group_disciplines(ctx, g)
    rng = random.Random(f"{ctx.seed}:current:{g.id}")
    if not found:  # группа без расписания — дисциплины из типового плана
        found = [CurrentDiscipline(n, 16, i < 3, rng.choice(ctx.teacher_pool) if ctx.teacher_pool else None)
                 for i, n in enumerate(curriculum.past_disciplines(g.speciality, semester))]

    # языки: каждый студент — в одной подгруппе каждого уровня («первый», «второй»)
    by_level: dict[str, list[str]] = defaultdict(list)
    for d in found:
        m = LANG_RE.match(d.name)
        if m:
            by_level[m.group(2).lower()].append(d.name)
    lang_members: dict[str, list[Student]] = defaultdict(list)
    for level, names in by_level.items():
        order = students[:]
        random.Random(f"{ctx.seed}:lang:{g.id}:{level}").shuffle(order)
        for i, s in enumerate(order):
            lang_members[names[i % len(names)]].append(s)

    elapsed_end = ctx.today
    for d in found:
        drng = random.Random(f"{ctx.seed}:course:{g.id}:{d.name}")
        language = d.name in lang_members
        members = lang_members[d.name] if language else students
        if not members:
            continue
        if d.has_exam:
            form = ControlForm.EXAM
        elif language:
            form = ControlForm.DIFF_CREDIT
        else:
            form = ControlForm.CREDIT if drng.random() < 0.55 else ControlForm.DIFF_CREDIT
        hours = max(36, min(216, 18 * round(d.lessons * 2 / 18)))
        course = Course(discipline_id=await _discipline_id(ctx, d.name), group_id=g.id, subgroup="",
                        teacher_id=d.teacher_id, semester=semester, academic_year=year, control_form=form,
                        hours=hours, credits=max(1, round(hours / 36)), status=CourseStatus.OPEN, created_at=ctx.now)
        ctx.db.add(course)
        await ctx.db.flush()
        await ctx.db.execute(insert(CourseEnrollment), [dict(course_id=course.id, student_id=s.id) for s in members])
        ctx.stats["courses"] += 1

        plan = _plan(form, language, year_start, semester, drng)
        if drng.random() < 0.2:  # часть преподавателей ещё не завела все точки
            plan = [p for p in plan if p[4] <= ctx.today + timedelta(days=10)] or plan[:1]
        difficulty = drng.gauss(0, 0.06)
        scores, events = [], []
        start = semester_start(year_start, semester)
        for pos, (kind, title, desc, mx, due) in enumerate(plan, start=1):
            a = Assessment(course_id=course.id, kind=kind, title=title, description=desc, max_points=float(mx),
                           due_date=due, position=pos, created_at=datetime.combine(start, datetime.min.time()))
            ctx.db.add(a)
            await ctx.db.flush()
            ctx.stats["assessments"] += 1
            if kind == AssessmentKind.SEMINAR:
                frac = _clip((elapsed_end - start).days / max(1, (due - start).days))
                if frac < 0.1:
                    continue
            elif due >= ctx.today:
                continue
            else:
                frac = 1.0
            for s in members:
                srng = random.Random(f"{ctx.seed}:score:{a.id}:{s.id}")
                level = ab[s.id][0]
                if kind != AssessmentKind.SEMINAR and srng.random() > 0.9 + 0.08 * level:
                    continue  # не сдал
                pts = _half(mx * frac * _clip(level + difficulty + srng.gauss(0, 0.12)))
                graded = datetime.combine(min(due + timedelta(days=srng.randint(1, 6)), ctx.today),
                                          datetime.min.time()) + timedelta(hours=srng.randint(9, 20))
                graded = min(graded, ctx.now - timedelta(hours=1, minutes=srng.randint(0, 600)))  # не в будущем
                scores.append(dict(assessment_id=a.id, student_id=s.id, points=pts, comment="",
                                   graded_by=d.teacher_id, graded_at=graded))
                if (ctx.today - graded.date()).days <= 21 and kind != AssessmentKind.SEMINAR:
                    events.append(dict(student_id=s.id, course_id=course.id, kind=GradeEventKind.SCORE,
                                       text=f"{d.name}: «{title}» — {scale.fmt_points(pts)} из {scale.fmt_points(mx)}",
                                       created_at=graded, notified_at=graded))
        if scores:
            await ctx.db.execute(insert(Score), scores)
            ctx.stats["scores"] += len(scores)
        if events:
            await ctx.db.execute(insert(GradeEvent), events)
            ctx.stats["events"] += len(events)


# --- прошлые семестры ---


async def past_semesters(ctx: Ctx, g: StudyGroup, students: list[Student], ab: dict[int, tuple[float, bool]],
                         faculty_teachers: list[int]) -> None:
    current = scale.semester_number(g.course or 1, ctx.today)
    if current <= 1:
        return
    intake = intake_year(g, ctx.today)
    pool = faculty_teachers or ctx.teacher_pool
    debt_left = {sid for sid, (_, debt) in ab.items() if debt}
    finals = []
    for sem in range(1, current):
        year_start = intake + (sem - 1) // 2
        rng = random.Random(f"{ctx.seed}:past:{g.id}:{sem}")
        session_day = date(year_start + 1, 1, 15) if sem % 2 else date(year_start + 1, 6, 14)
        names = curriculum.past_disciplines(g.speciality, sem)
        for i, name in enumerate(names):
            if "физическая культура" in name.lower():
                form = ControlForm.CREDIT
            elif i < 3:
                form = ControlForm.EXAM
            else:
                form = rng.choice((ControlForm.CREDIT, ControlForm.CREDIT, ControlForm.DIFF_CREDIT))
            teacher = rng.choice(pool) if pool else None
            hours = rng.choice((72, 108, 144)) if form == ControlForm.EXAM else rng.choice((36, 72, 108))
            course = Course(discipline_id=await _discipline_id(ctx, name), group_id=g.id, subgroup="", teacher_id=teacher,
                            semester=sem, academic_year=f"{year_start}/{year_start + 1}", control_form=form, hours=hours,
                            credits=hours // 36, status=CourseStatus.CLOSED,
                            closed_at=datetime.combine(session_day + timedelta(days=14), datetime.min.time()),
                            created_at=ctx.now)
            ctx.db.add(course)
            await ctx.db.flush()
            ctx.stats["courses"] += 1
            await ctx.db.execute(insert(CourseEnrollment), [dict(course_id=course.id, student_id=s.id) for s in students])
            difficulty = rng.gauss(0, 0.05)
            for s in students:
                srng = random.Random(f"{ctx.seed}:final:{course.id}:{s.id}")
                level, _ = ab[s.id]
                day = session_day + timedelta(days=srng.randint(0, 10))
                attempts = []
                forced_debt = s.id in debt_left and sem == current - 1 and form == ControlForm.EXAM
                if forced_debt:
                    debt_left.discard(s.id)
                    attempts.append((55 * 0.45, None, FinalResult.FAIL, day))
                else:
                    for attempt in range(1, 4):
                        bonus = 0.12 * (attempt - 1)  # к пересдаче готовятся лучше
                        if form == ControlForm.EXAM:
                            cur = _half(60 * _clip(level + difficulty + srng.gauss(0, 0.1)))
                            if cur < scale.ADMISSION_MIN and attempt == 1 and srng.random() < 0.5:
                                cur = scale.ADMISSION_MIN + srng.randint(0, 4)  # «добрал» к сессии
                            exam = _half(40 * _clip(level + bonus + difficulty + srng.gauss(0, 0.14)))
                            res = scale.suggest(form, cur, exam) if cur >= scale.ADMISSION_MIN else FinalResult.FAIL
                        else:
                            cur = _half(100 * _clip(level + bonus + 0.04 + difficulty + srng.gauss(0, 0.1)))
                            exam = None
                            res = scale.suggest(form, cur)
                        if attempt == 1 and srng.random() < 0.015:
                            res = FinalResult.ABSENT
                        attempts.append((cur, exam, res, day + timedelta(days=21 * (attempt - 1))))
                        if res not in FinalResult.NEGATIVE:
                            break
                        # долг остаётся только в последнем прошедшем семестре и только у слабых
                        last = sem == current - 1
                        if attempt == 1 and last and level < 0.55 and srng.random() < 0.5:
                            break
                for n, (cur, exam, res, when) in enumerate(attempts, start=1):
                    finals.append(dict(course_id=course.id, student_id=s.id, attempt=n, current_points=cur,
                                       exam_points=exam, total_points=cur + (exam or 0), result=res,
                                       teacher_id=teacher, graded_on=when))
    if finals:
        for i in range(0, len(finals), 5000):
            await ctx.db.execute(insert(FinalGrade), finals[i:i + 5000])
        ctx.stats["final_grades"] += len(finals)


async def faculty_teachers(ctx: Ctx, faculty_id: int | None) -> list[int]:
    if faculty_id is None:
        return []
    rows = await ctx.db.scalars(
        select(lesson_teachers.c.teacher_id).distinct()
        .join(LessonGroup, LessonGroup.lesson_id == lesson_teachers.c.lesson_id)
        .join(StudyGroup, StudyGroup.id == LessonGroup.group_id)
        .where(StudyGroup.faculty_id == faculty_id)
    )
    return sorted(rows.all())


async def enroll_missing(ctx: Ctx, g: StudyGroup, students: list[Student]) -> None:
    """Группа уже сгенерирована, но в ней появился новый студент (например, демо после seed)."""
    year = scale.academic_year(ctx.today)
    courses = list((await ctx.db.scalars(
        select(Course).where(Course.group_id == g.id, Course.academic_year == year)
    )).all())
    if not courses:
        return
    have = set((await ctx.db.execute(
        select(CourseEnrollment.course_id, CourseEnrollment.student_id)
        .where(CourseEnrollment.course_id.in_([c.id for c in courses]))
    )).all())
    names = {c.id: n for c, n in (await ctx.db.execute(
        select(Course, Discipline.name).join(Discipline, Discipline.id == Course.discipline_id)
        .where(Course.id.in_([c.id for c in courses])))).all()}
    enrolled = {sid for _, sid in have}
    rows = []
    for s in students:
        if s.id in enrolled:
            continue
        for c in courses:
            if LANG_RE.match(names[c.id]) and not s.record_book.startswith(GEN_PREFIX):
                # в языковые подгруппы — в первую по алфавиту каждого уровня
                level = LANG_RE.match(names[c.id]).group(2).lower()
                first = min((n for n in names.values() if LANG_RE.match(n) and LANG_RE.match(n).group(2).lower() == level))
                if names[c.id] != first:
                    continue
            rows.append(dict(course_id=c.id, student_id=s.id))
    if rows:
        await ctx.db.execute(insert(CourseEnrollment), rows)
        ctx.stats["late_enrollments"] += len(rows)


# --- сброс ---


async def reset(db: AsyncSession) -> None:
    from app.models.document_notice import DocumentRecipient

    gen_students = select(Student.id).where(Student.record_book.like(f"{GEN_PREFIX}%"))
    gen_users = select(User.id).where(User.student_id.in_(gen_students))
    for stmt in (
        # Сначала зависимые уведомления: чужие адресаты и общие объявления сохраняются.
        delete(Announcement).where(Announcement.student_id.in_(gen_students)),
        delete(NotificationRead).where(NotificationRead.user_id.in_(gen_users)),
        delete(DocumentRecipient).where(DocumentRecipient.student_id.in_(gen_students)),
        delete(GradeEvent), delete(FinalGrade), delete(Score), delete(Assessment), delete(CourseEnrollment),
        delete(Course), delete(AuthSession).where(AuthSession.user_id.in_(gen_users)),
        delete(User).where(User.student_id.in_(gen_students)), delete(Student).where(Student.id.in_(gen_students)),
    ):
        await db.execute(stmt)


# --- точка входа ---


async def extend_group(ctx: Ctx, g: StudyGroup, n: int, hashes: list[str], faculty: Faculty | None) -> None:
    """Дополнить синтетическую группу, сохранив существующие ведомости и правки.

    Новые студенты получают копию вымышленного учебного профиля одного из уже
    сгенерированных одногруппников, включая его языковые подгруппы и историю.
    """
    peers = list((await ctx.db.scalars(select(Student).where(Student.study_group_id == g.id,
        Student.record_book.like(f"{GEN_PREFIX}%")).order_by(Student.id))).all())
    start = max(int(s.record_book.rsplit("-", 1)[1]) for s in peers) + 1
    ids = await create_students(ctx, g, n, hashes, faculty, start=start)
    for i, sid in enumerate(ids):
        peer = peers[i % len(peers)]
        for model, fields in (
            (CourseEnrollment, ("course_id",)),
            (Score, ("assessment_id", "points", "comment", "graded_by", "graded_at")),
            (FinalGrade, ("course_id", "attempt", "current_points", "exam_points", "total_points",
                          "result", "teacher_id", "graded_on")),
        ):
            source = (await ctx.db.execute(select(*(getattr(model, f) for f in fields))
                .where(model.student_id == peer.id))).all()
            if source:
                await ctx.db.execute(insert(model), [dict(zip(fields, row), student_id=sid) for row in source])
                ctx.stats[{CourseEnrollment: "enrollments", Score: "scores", FinalGrade: "final_grades"}[model]] += len(source)
    ctx.stats["groups_extended"] += 1


async def generate(*, students: int, per_group: int, seed: str, do_reset: bool, only: list[str],
                   today: date | None = None, database_url: str | None = None,
                   demo_password: str | None = None, fill_existing: bool = False,
                   access_password: str | None = None) -> Counter:
    if database_url is None:
        cfg = get_settings()
        url, demo_password = cfg.database_url, cfg.demo_password if demo_password is None else demo_password
    else:
        url = database_url
    engine = make_engine(url)
    factory = make_session_factory(engine)
    stats: Counter = Counter()
    started = time.monotonic()
    try:
        async with factory() as db:
            if do_reset:
                await reset(db)
                await db.commit()
                print("сброс: сгенерированные студенты и вся успеваемость удалены")
            groups = await pick_groups(db, students, per_group, only)
            gen_counts = dict((await db.execute(
                select(Student.study_group_id, func.count()).where(Student.record_book.like(f"{GEN_PREFIX}%"))
                .group_by(Student.study_group_id)
            )).all())
            todo = [g for g in groups if not gen_counts.get(g.id)]
            count = sum(max(0, per_group - gen_counts.get(g.id, 0)) if fill_existing else
                        (0 if gen_counts.get(g.id) else per_group) for g in groups)
            print(f"групп: {len(groups)}, из них новых {len(todo)}; новых студентов {count}")
            # Один общий хэш допустим только для явно включённого синтетического стенда.
            hashes = [hash_password(access_password)] * count if access_password and count else make_password_hashes(count)
            ctx = Ctx(db=db, today=today or date.today(), seed=seed,
                      disciplines=dict((await db.execute(select(Discipline.name, Discipline.id))).all()),
                      teacher_pool=sorted((await db.scalars(select(Teacher.id))).all()), stats=stats, now=utcnow())
            fac_cache: dict[int | None, list[int]] = {}
            for n, g in enumerate(groups, start=1):
                fresh = not gen_counts.get(g.id)
                faculty = await db.get(Faculty, g.faculty_id) if g.faculty_id else None
                if not fresh and fill_existing and gen_counts[g.id] < per_group:
                    await extend_group(ctx, g, per_group - gen_counts[g.id], hashes, faculty)
                if fresh:
                    await create_students(ctx, g, per_group, hashes, faculty)
                members = list((await db.scalars(
                    select(Student).where(Student.study_group_id == g.id).order_by(Student.full_name)
                )).all())
                if not fresh:
                    await enroll_missing(ctx, g, members)
                    stats["groups_skipped"] += 1
                else:
                    ab = abilities(ctx, members)
                    await current_courses(ctx, g, members, ab)
                    if g.faculty_id not in fac_cache:
                        fac_cache[g.faculty_id] = await faculty_teachers(ctx, g.faculty_id)
                    await past_semesters(ctx, g, members, ab, fac_cache[g.faculty_id])
                    stats["groups"] += 1
                await db.commit()
                if n % 10 == 0 or n == len(groups):
                    print(f"  {n}/{len(groups)} групп · студентов {stats['students']} · баллов {stats['scores']} · "
                          f"итогов {stats['final_grades']} · {time.monotonic() - started:.0f} с")
        from app.seed import link_demo_teachers
        async with factory() as db:
            linked = await link_demo_teachers(db, demo_password or "")
            await db.commit()
        if linked:
            print("учётки преподавателей:", ", ".join(f"{login} → {name}" for login, name in linked))
    finally:
        await engine.dispose()
    return stats


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Вымышленные студенты и успеваемость (решение 0007)")
    p.add_argument("--students", type=int, default=3000, help="сколько студентов создать всего (по умолчанию 3000)")
    p.add_argument("--per-group", type=int, default=12, help="студентов в группе (по умолчанию 12)")
    p.add_argument("--group", action="append", default=[], help="только эти группы (24МЖ-02 или 3МЖ-02)")
    p.add_argument("--seed", default="mgimo-lms-2026", help="зерно генератора")
    p.add_argument("--reset", action="store_true", help="удалить сгенерированных студентов и ВСЮ успеваемость перед запуском")
    args = p.parse_args(argv)
    if args.per_group < 1 or args.per_group > 40:
        sys.exit("--per-group — от 1 до 40")
    stats = asyncio.run(generate(students=args.students, per_group=args.per_group, seed=args.seed,
                                 do_reset=args.reset, only=args.group))
    print("готово:", ", ".join(f"{k} {v}" for k, v in sorted(stats.items())))
