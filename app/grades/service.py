"""Успеваемость: ведомости преподавателя и зачётка студента. Без привязки к FastAPI.

Правило доступа (CLAUDE.md, «Студент видит только своё»): каждая функция получает id из сессии
(`teacher_id` или `student_id`) и фильтрует по нему в самом SQL-запросе. Чужой курс,
контрольная точка или студент не из ведомости — `None` (экран отвечает 404), а не исключение
с подробностями.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import utcnow
from app.cache import cached, get_cache
from app.cache.invalidation import SCHEDULE
from app.grades import scale
from app.grades.queries import latest_final_clause
from app.models import Lesson, LessonGroup, Student, StudyGroup, Teacher
from app.models.grades import (
    Assessment,
    AssessmentKind,
    ControlForm,
    Course,
    CourseEnrollment,
    CourseStatus,
    Discipline,
    FinalGrade,
    FinalResult,
    GradeEvent,
    GradeEventKind,
    Score,
)


class ValidationError(ValueError):
    """Ошибка ввода, текст показывается преподавателю как есть."""


class Locked(ValidationError):
    """Ведомость закрыта — менять нельзя."""


# --- общие кусочки ---


@dataclass
class CourseCard:
    course: Course
    discipline: str
    group: str
    """Номер группы по курсу, как в РУЗ: «3МЖ-02»."""
    teacher: str
    students: int = 0
    graded: int = 0
    """Сколько оценок уже выставлено (строк scores)."""
    assessments: int = 0
    avg_percent: float | None = None
    next_due: date | None = None

    @property
    def form_title(self) -> str:
        return ControlForm.TITLES.get(self.course.control_form, self.course.control_form)

    @property
    def title(self) -> str:
        return f"{self.discipline} · {self.group}" + (f" ({self.course.subgroup})" if self.course.subgroup else "")


def _course_query():
    return (
        select(Course, Discipline.name, StudyGroup.number, Teacher.full_name)
        .join(Discipline, Discipline.id == Course.discipline_id)
        .join(StudyGroup, StudyGroup.id == Course.group_id)
        .outerjoin(Teacher, Teacher.id == Course.teacher_id)
    )


def _card(row) -> CourseCard:
    course, discipline, group, teacher = row
    return CourseCard(course=course, discipline=discipline, group=group or "", teacher=teacher or "")


async def _roster(db: AsyncSession, course_id: int, student_id: int | None = None) -> list[Student]:
    q = (
        select(Student)
        .join(CourseEnrollment, CourseEnrollment.student_id == Student.id)
        .where(CourseEnrollment.course_id == course_id)
        .order_by(Student.full_name)
    )
    if student_id is not None:
        q = q.where(Student.id == student_id)
    return list((await db.scalars(q)).all())


async def current_points(db: AsyncSession, course_id: int, student_ids: list[int] | None = None) -> dict[int, float]:
    q = (
        select(Score.student_id, func.sum(Score.points))
        .join(Assessment, Assessment.id == Score.assessment_id)
        .where(Assessment.course_id == course_id)
        .group_by(Score.student_id)
    )
    if student_ids is not None:
        q = q.where(Score.student_id.in_(student_ids))
    return {sid: float(total or 0) for sid, total in (await db.execute(q)).all()}


async def _latest_finals(db: AsyncSession, course_ids: list[int], student_id: int | None = None) -> dict[tuple[int, int], FinalGrade]:
    """Последняя попытка по каждой паре (курс, студент)."""
    if not course_ids:
        return {}
    q = select(FinalGrade).where(FinalGrade.course_id.in_(course_ids), latest_final_clause())
    if student_id is not None:
        q = q.where(FinalGrade.student_id == student_id)
    out: dict[tuple[int, int], FinalGrade] = {}
    for fg in (await db.scalars(q.order_by(FinalGrade.attempt))).all():
        out[(fg.course_id, fg.student_id)] = fg
    return out


def counts_now(a: Assessment, has_score: bool, today: date) -> bool:
    """Входит ли точка в «возможный на сегодня» максимум: срок прошёл или балл уже стоит.
    Работа на семинарах копится весь семестр — до её срока в процент не идёт."""
    if a.kind == AssessmentKind.SEMINAR and (a.due_date is None or a.due_date >= today):
        return False
    return has_score or (a.due_date is not None and a.due_date < today)


def _event(db: AsyncSession, student_id: int, course_id: int, kind: str, text: str) -> None:
    db.add(GradeEvent(student_id=student_id, course_id=course_id, kind=kind, text=text[:500], created_at=utcnow()))


def _parse_number(raw: str | float | None, *, what: str, lo: float, hi: float, lo_inclusive: bool = True) -> float | None:
    """Число из поля формы: запятая или точка, пусто — None. Не число или вне диапазона — ValidationError."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip().replace(",", ".")
        if raw == "":
            return None
        try:
            raw = float(raw)
        except ValueError:
            raise ValidationError(f"{what} — это число, например 7 или 7,5")
    value = float(raw)
    too_low = value < lo if lo_inclusive else value <= lo
    if value != value or too_low or value > hi:  # value != value — NaN
        raise ValidationError(f"{what} — от {scale.fmt_points(lo)} до {scale.fmt_points(hi)}")
    return value


def _parse_points(raw: str | float | None, max_points: float) -> float | None:
    value = _parse_number(raw, what="Баллы", lo=0, hi=max_points)
    return None if value is None else round(value * 2) / 2  # шаг полбалла


# --- преподаватель: список курсов ---


async def teacher_courses(db: AsyncSession, teacher_id: int, academic_year: str | None = None,
                          today: date | None = None) -> list[CourseCard]:
    today = today or date.today()
    q = _course_query().where(Course.teacher_id == teacher_id)
    if academic_year:
        q = q.where(Course.academic_year == academic_year)
    cards = [_card(r) for r in (await db.execute(q.order_by(Course.semester.desc(), Discipline.name, StudyGroup.number))).all()]
    if not cards:
        return []
    ids = [c.course.id for c in cards]
    students = dict((await db.execute(
        select(CourseEnrollment.course_id, func.count()).where(CourseEnrollment.course_id.in_(ids))
        .group_by(CourseEnrollment.course_id)
    )).all())
    counts = dict((await db.execute(
        select(Assessment.course_id, func.count()).where(Assessment.course_id.in_(ids)).group_by(Assessment.course_id)
    )).all())
    graded = {cid: (n, pts, mx) for cid, n, pts, mx in (await db.execute(
        select(Assessment.course_id, func.count(Score.id), func.sum(Score.points), func.sum(Assessment.max_points))
        .join(Score, Score.assessment_id == Assessment.id)
        .where(Assessment.course_id.in_(ids),
               # работа на семинарах копится до конца семестра — в средний процент не идёт
               ~and_(Assessment.kind == AssessmentKind.SEMINAR, Assessment.due_date >= today))
        .group_by(Assessment.course_id)
    )).all()}
    due = dict((await db.execute(
        select(Assessment.course_id, func.min(Assessment.due_date))
        .where(Assessment.course_id.in_(ids), Assessment.due_date >= today)
        .group_by(Assessment.course_id)
    )).all())
    for c in cards:
        cid = c.course.id
        c.students = int(students.get(cid, 0))
        c.assessments = int(counts.get(cid, 0))
        c.graded, pts, mx = graded.get(cid, (0, 0, 0))
        if mx:
            c.avg_percent = round(100 * float(pts or 0) / float(mx), 1)
        c.next_due = due.get(cid)
    return cards


async def get_course(db: AsyncSession, teacher_id: int, course_id: int) -> CourseCard | None:
    row = (await db.execute(_course_query().where(Course.id == course_id, Course.teacher_id == teacher_id))).first()
    return _card(row) if row else None


# --- преподаватель: журнал курса ---


@dataclass
class JournalRow:
    student: Student
    scores: dict[int, Score] = field(default_factory=dict)
    current: float = 0.0
    possible: float = 0.0
    """Максимум по точкам, которые уже можно было сдать (см. counts_now)."""
    final: FinalGrade | None = None
    suggested: str = ""

    @property
    def admitted(self) -> bool:
        return self.current >= scale.ADMISSION_MIN


@dataclass
class Journal:
    card: CourseCard
    assessments: list[Assessment]
    rows: list[JournalRow]
    current_max: float
    planned_max: float
    """Сумма максимумов уже созданных контрольных точек."""

    @property
    def locked(self) -> bool:
        return self.card.course.status == CourseStatus.CLOSED

    @property
    def is_exam(self) -> bool:
        return self.card.course.control_form == ControlForm.EXAM

    @property
    def at_risk(self) -> list[JournalRow]:
        """Кто рискует не получить допуск или зачёт: меньше 55 % от уже возможного."""
        return [r for r in self.rows if r.possible and r.current < 0.55 * r.possible]


async def journal(db: AsyncSession, teacher_id: int, course_id: int, today: date | None = None,
                  *, student_id: int | None = None) -> Journal | None:
    """Журнал курса. `student_id` — только одна строка (ответ HTMX после сохранения итога)."""
    today = today or date.today()
    card = await get_course(db, teacher_id, course_id)
    if card is None:
        return None
    assessments = list((await db.scalars(
        select(Assessment).where(Assessment.course_id == course_id)
        .order_by(Assessment.position, Assessment.due_date, Assessment.id)
    )).all())
    students = await _roster(db, course_id, student_id)
    rows = {s.id: JournalRow(student=s) for s in students}
    if assessments and rows:
        sq = select(Score).where(Score.assessment_id.in_([a.id for a in assessments]), Score.student_id.in_(list(rows)))
        for sc in (await db.scalars(sq)).all():
            row = rows.get(sc.student_id)
            if row is not None:
                row.scores[sc.assessment_id] = sc
                row.current += sc.points
    finals = await _latest_finals(db, [course_id], student_id)
    form = card.course.control_form
    for r in rows.values():
        r.possible = sum(a.max_points for a in assessments if counts_now(a, a.id in r.scores, today))
        r.final = finals.get((course_id, r.student.id))
        r.suggested = scale.suggest(form, r.current, r.final.exam_points if r.final else None)
    return Journal(card=card, assessments=assessments, rows=list(rows.values()),
                   current_max=scale.current_max(form), planned_max=sum(a.max_points for a in assessments))


async def get_assessment(db: AsyncSession, teacher_id: int, course_id: int, assessment_id: int) -> Assessment | None:
    return await db.scalar(
        select(Assessment).join(Course, Course.id == Assessment.course_id)
        .where(Assessment.id == assessment_id, Assessment.course_id == course_id, Course.teacher_id == teacher_id)
    )


async def create_assessment(
    db: AsyncSession, teacher_id: int, course_id: int, *, kind: str, title: str, description: str = "",
    max_points: str | float = 10, due_date: str | date | None = None,
) -> Assessment | None:
    card = await get_course(db, teacher_id, course_id)
    if card is None:
        return None
    if card.course.status == CourseStatus.CLOSED:
        raise Locked("Ведомость закрыта — новые задания добавить нельзя")
    if kind not in AssessmentKind.TITLES:
        raise ValidationError("Неизвестный тип задания")
    title = title.strip()
    if not title:
        raise ValidationError("Укажите название задания")
    if len(title) > 200:
        raise ValidationError("Название — не длиннее 200 символов")
    if len(description) > 5000:
        raise ValidationError("Описание — не длиннее 5000 символов")
    mp = _parse_number(max_points, what="Максимум баллов", lo=0, hi=100, lo_inclusive=False)
    if mp is None:
        raise ValidationError("Укажите максимум баллов")
    due: date | None
    if isinstance(due_date, str):
        try:
            due = date.fromisoformat(due_date) if due_date.strip() else None
        except ValueError:
            raise ValidationError("Неверная дата")
    else:
        due = due_date
    if kind == AssessmentKind.HOMEWORK and due is None:
        raise ValidationError("У домашнего задания должен быть срок сдачи")
    position = (await db.scalar(select(func.max(Assessment.position)).where(Assessment.course_id == course_id))) or 0
    a = Assessment(course_id=course_id, kind=kind, title=title, description=description.strip(), max_points=mp,
                   due_date=due, position=position + 1, created_at=utcnow())
    db.add(a)
    await db.flush()
    if kind == AssessmentKind.HOMEWORK:
        when = due.strftime("%d.%m") if due else ""
        for s in await _roster(db, course_id):
            _event(db, s.id, course_id, GradeEventKind.HOMEWORK, f"{card.discipline}: новое задание «{title}», сдать до {when}")
    return a


async def delete_assessment(db: AsyncSession, teacher_id: int, course_id: int, assessment_id: int) -> bool:
    a = await get_assessment(db, teacher_id, course_id, assessment_id)
    if a is None:
        return False
    card = await get_course(db, teacher_id, course_id)
    if card and card.course.status == CourseStatus.CLOSED:
        raise Locked("Ведомость закрыта")
    from app.models.learning import Submission, TaskMessage
    await db.execute(delete(Submission).where(Submission.assessment_id == a.id))
    await db.execute(delete(TaskMessage).where(TaskMessage.assessment_id == a.id))
    await db.execute(delete(Score).where(Score.assessment_id == a.id))
    await db.delete(a)
    # «Новое задание» и баллы по нему уже в ленте студентов, а GradeEvent не ссылается на задание,
    # поэтому вместо удаления старых записей сообщаем об отмене.
    if card is not None:
        for s in await _roster(db, course_id):
            _event(db, s.id, course_id, GradeEventKind.HOMEWORK,
                   f"{card.discipline}: задание «{a.title}» отменено, баллы по нему сняты")
    return True


async def _enrolled(db: AsyncSession, course_id: int, student_id: int) -> bool:
    return bool(await db.scalar(
        select(func.count()).select_from(CourseEnrollment)
        .where(CourseEnrollment.course_id == course_id, CourseEnrollment.student_id == student_id)
    ))


async def set_score(
    db: AsyncSession, teacher_id: int, course_id: int, assessment_id: int, student_id: int,
    points: str | float | None, comment: str | None = None,
) -> tuple[Score | None, float] | None:
    """Ставит, меняет или (пустое значение) удаляет балл. Возвращает (балл, новый накоп студента).

    comment=None — комментарий не меняется (ячейка журнала его не отправляет), "" — стирается.

    None — курс не свой, точки нет в курсе или студент не записан в ведомость (экран отвечает 404).
    """
    a = await get_assessment(db, teacher_id, course_id, assessment_id)
    if a is None or not await _enrolled(db, course_id, student_id):
        return None
    card = await get_course(db, teacher_id, course_id)
    assert card is not None
    if card.course.status == CourseStatus.CLOSED:
        raise Locked("Ведомость закрыта — баллы не меняются")
    value = _parse_points(points, a.max_points)
    existing = await db.scalar(select(Score).where(Score.assessment_id == a.id, Score.student_id == student_id))
    if value is None:
        if existing is not None:
            await db.delete(existing)
        score = None
    else:
        if existing is None:
            # Параллельный запрос мог вставить ту же ячейку — тогда обновляем его строку.
            existing = await _insert_score(db, a.id, student_id, value, comment, teacher_id)
        if existing is None:
            score = await db.scalar(select(Score).where(Score.assessment_id == a.id, Score.student_id == student_id))
        else:
            changed = existing.points != value
            existing.points = value
            if comment is not None:
                existing.comment = comment[:500]
            existing.graded_by, existing.graded_at = teacher_id, utcnow()
            score = existing
            if not changed:
                await db.flush()
                return score, (await current_points(db, course_id, [student_id])).get(student_id, 0.0)
    await db.flush()
    if score is not None:
        _event(db, student_id, course_id, GradeEventKind.SCORE,
               f"{card.discipline}: «{a.title}» — {scale.fmt_points(value)} из {scale.fmt_points(a.max_points)}")
    return score, (await current_points(db, course_id, [student_id])).get(student_id, 0.0)


async def _insert_score(db: AsyncSession, assessment_id: int, student_id: int, value: float,
                        comment: str | None, teacher_id: int) -> Score | None:
    """Вставляет балл в savepoint. None — вставлено. Если ту же ячейку уже вставил параллельный
    запрос (uq_scores_assessment_student), возвращает его строку вместо ошибки 500."""
    try:
        async with db.begin_nested():
            db.add(Score(assessment_id=assessment_id, student_id=student_id, points=value,
                         comment=(comment or "")[:500], graded_by=teacher_id, graded_at=utcnow()))
        return None
    except IntegrityError:
        return await db.scalar(select(Score).where(Score.assessment_id == assessment_id, Score.student_id == student_id))


async def set_final(
    db: AsyncSession, teacher_id: int, course_id: int, student_id: int, *, result: str,
    exam_points: str | float | None = None, on: date | None = None,
) -> FinalGrade | None:
    """Итог в зачётку. Пока ведомость открыта — правится основная попытка; после закрытия
    можно только добавить пересдачу тому, у кого последняя попытка неудачная."""
    card = await get_course(db, teacher_id, course_id)
    if card is None or not await _enrolled(db, course_id, student_id):
        return None
    form = card.course.control_form
    if result not in scale.allowed_results(form):
        raise ValidationError("Такой итог для этой формы контроля не выставляется")
    exam = _parse_points(exam_points, scale.EXAM_MAX) if form == ControlForm.EXAM else None
    current = (await current_points(db, course_id, [student_id])).get(student_id, 0.0)
    if form == ControlForm.EXAM and result in FinalResult.NUMERIC and current < scale.ADMISSION_MIN:
        raise ValidationError(f"Нет допуска: накоп {scale.fmt_points(current)} из {scale.fmt_points(scale.ADMISSION_MIN)} необходимых")
    latest = (await _latest_finals(db, [course_id], student_id)).get((course_id, student_id))
    today = on or date.today()
    total = current + (exam or 0.0)
    if card.course.status == CourseStatus.CLOSED:
        if latest is None or latest.result not in FinalResult.NEGATIVE:
            raise Locked("Ведомость закрыта — итог меняется только пересдачей")
        if latest.attempt >= 3:
            raise ValidationError("Использованы все три попытки — дальше решает комиссия деканата")
        attempt: int | None = latest.attempt + 1
        label = f"пересдача {latest.attempt}"
    elif latest is None:
        attempt, label = 1, ControlForm.TITLES[form].lower()
    else:
        attempt = None
    if attempt is not None:
        fg = FinalGrade(course_id=course_id, student_id=student_id, attempt=attempt, current_points=current,
                        exam_points=exam, total_points=total, result=result, teacher_id=teacher_id, graded_on=today)
        db.add(fg)
    else:
        fg = latest
        fg.current_points, fg.exam_points, fg.total_points = current, exam, total
        fg.result, fg.teacher_id, fg.graded_on = result, teacher_id, today
        label = ControlForm.TITLES[form].lower()
    await db.flush()
    _event(db, student_id, course_id, GradeEventKind.FINAL,
           f"{card.discipline}: {label} — {FinalResult.TITLES[result].lower()} ({scale.fmt_points(total)} б.)")
    return fg


async def close_course(db: AsyncSession, teacher_id: int, course_id: int, today: date | None = None) -> Course | None:
    j = await journal(db, teacher_id, course_id, today)
    if j is None:
        return None
    if j.locked:
        return j.card.course
    missing = [r for r in j.rows if r.final is None]
    if missing:
        raise ValidationError(f"Итог не выставлен у {len(missing)} студентов — закрыть ведомость нельзя")
    j.card.course.status = CourseStatus.CLOSED
    j.card.course.closed_at = utcnow()
    return j.card.course


@dataclass
class HomeworkItem:
    assessment: Assessment
    card: CourseCard
    submitted: int
    """Сколько студентов уже получили балл (= работа проверена)."""
    students: int


async def teacher_homework(db: AsyncSession, teacher_id: int, academic_year: str,
                           today: date | None = None) -> list[HomeworkItem]:
    cards = await teacher_courses(db, teacher_id, academic_year, today)
    return await homework_for_courses(db, cards)


async def homework_for_courses(
    db: AsyncSession, course_cards: list[CourseCard], *, due_between: tuple[date, date] | None = None,
) -> list[HomeworkItem]:
    """Домашние задания для ведомостей, уже отобранных по преподавателю."""
    cards = {card.course.id: card for card in course_cards}
    if not cards:
        return []
    query = select(Assessment).where(
        Assessment.course_id.in_(cards), Assessment.kind == AssessmentKind.HOMEWORK,
    )
    if due_between is not None:
        query = query.where(Assessment.due_date.between(*due_between))
    items = (await db.scalars(query.order_by(Assessment.due_date.desc(), Assessment.id.desc()))).all()
    counts = dict((await db.execute(
        select(Score.assessment_id, func.count()).where(Score.assessment_id.in_([a.id for a in items]))
        .group_by(Score.assessment_id)
    )).all()) if items else {}
    return [HomeworkItem(assessment=a, card=cards[a.course_id], submitted=int(counts.get(a.id, 0)),
                         students=cards[a.course_id].students) for a in items]


# --- студент: зачётка и текущий семестр ---


@dataclass
class StudentAssessment:
    assessment: Assessment
    score: Score | None


@dataclass
class StudentCourse:
    card: CourseCard
    assessments: list[StudentAssessment]
    current: float
    planned_max: float
    current_max: float
    final: FinalGrade | None
    attempts: int
    possible: float = 0.0
    earned: float = 0.0
    """Баллы и максимум только по точкам, которые уже можно было сдать (см. counts_now)."""
    today: date = field(default_factory=date.today)
    """Дата, на которую собрана зачётка (record_book): от неё считаются сроки ДЗ."""

    @property
    def percent(self) -> float | None:
        return round(100 * self.earned / self.possible, 1) if self.possible else None

    @property
    def homework_due(self) -> list[StudentAssessment]:
        return [a for a in self.assessments if a.assessment.kind == AssessmentKind.HOMEWORK and a.score is None
                and a.assessment.due_date and a.assessment.due_date >= self.today]

    @property
    def debt(self) -> bool:
        return self.final is not None and self.final.result in FinalResult.NEGATIVE

    @property
    def suggested(self) -> str:
        return scale.suggest(self.card.course.control_form, self.current)


@dataclass
class Semester:
    number: int
    academic_year: str
    courses: list[StudentCourse]

    @property
    def title(self) -> str:
        season = "осенний" if self.number % 2 else "весенний"
        return f"{self.number}-й семестр · {self.academic_year}, {season}"


@dataclass
class RecordBook:
    current: Semester | None
    past: list[Semester]
    gpa: float | None
    debts: list[StudentCourse]
    next_exam: tuple[date, str] | None
    events: list[GradeEvent]

    @property
    def current_percent(self) -> float | None:
        if not self.current:
            return None
        vals = [c.percent for c in self.current.courses if c.percent is not None]
        return round(sum(vals) / len(vals), 1) if vals else None


async def _next_exam(db: AsyncSession, student: Student, today: date) -> tuple[date, str] | None:
    """Ближайший экзамен группы из расписания — через кэш расписания (решение 0027)."""

    async def load() -> tuple[date, str] | None:
        found = (await db.execute(
            select(Lesson.date, Lesson.discipline).join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
            .where(LessonGroup.group_id == student.study_group_id, Lesson.date >= today,
                   or_(Lesson.kind_of_work.like("Экзам%"), Lesson.kind_of_work.like("экзам%")))
            .order_by(Lesson.date).limit(1)
        )).first()
        return (found[0], found[1]) if found else None

    cache = get_cache()
    gens = await cache.generations(SCHEDULE)
    if gens is None:
        return await load()
    key = f"exam:s{student.id}:g{student.study_group_id}:{today.isoformat()}:{gens[0]}"
    return await cached(key, cache.schedule_ttl, load,
                        dump=lambda v: {"exam": [v[0].isoformat(), v[1]] if v else None},
                        restore=lambda raw: (date.fromisoformat(raw["exam"][0]), raw["exam"][1]) if raw["exam"] else None)


async def record_book(db: AsyncSession, student_id: int, today: date | None = None, *, events: int = 8) -> RecordBook:
    """Всё, что видит студент в «Успеваемости». Только курсы, куда записан студент из сессии."""
    today = today or date.today()
    rows = (await db.execute(
        _course_query().join(CourseEnrollment, CourseEnrollment.course_id == Course.id)
        .where(CourseEnrollment.student_id == student_id)
        .order_by(Course.semester.desc(), Discipline.name)
    )).all()
    cards = [_card(r) for r in rows]
    ids = [c.course.id for c in cards]
    assessments: dict[int, list[Assessment]] = defaultdict(list)
    scores: dict[int, Score] = {}
    if ids:
        for a in (await db.scalars(select(Assessment).where(Assessment.course_id.in_(ids))
                                   .order_by(Assessment.position, Assessment.id))).all():
            assessments[a.course_id].append(a)
        all_aids = [a.id for lst in assessments.values() for a in lst]
        if all_aids:
            for sc in (await db.scalars(select(Score).where(Score.student_id == student_id,
                                                            Score.assessment_id.in_(all_aids)))).all():
                scores[sc.assessment_id] = sc
    finals = await _latest_finals(db, ids, student_id)
    attempts = dict((await db.execute(
        select(FinalGrade.course_id, func.count()).where(FinalGrade.student_id == student_id)
        .group_by(FinalGrade.course_id)
    )).all()) if ids else {}

    year = scale.academic_year(today)
    by_sem: dict[tuple[int, str], list[StudentCourse]] = defaultdict(list)
    for c in cards:
        lst = [StudentAssessment(a, scores.get(a.id)) for a in assessments.get(c.course.id, [])]
        now = [x for x in lst if counts_now(x.assessment, x.score is not None, today)]
        sc = StudentCourse(
            possible=sum(x.assessment.max_points for x in now), earned=sum(x.score.points for x in now if x.score),
            card=c, assessments=lst, current=sum(x.score.points for x in lst if x.score),
            planned_max=sum(x.assessment.max_points for x in lst), current_max=scale.current_max(c.course.control_form),
            final=finals.get((c.course.id, student_id)), attempts=int(attempts.get(c.course.id, 0)), today=today,
        )
        by_sem[(c.course.semester, c.course.academic_year)].append(sc)
    semesters = [Semester(n, y, lst) for (n, y), lst in sorted(by_sem.items(), reverse=True)]
    current = next((s for s in semesters if s.academic_year == year and
                    any(c.card.course.status == CourseStatus.OPEN for c in s.courses)), None)
    past = [s for s in semesters if s is not current]

    numeric = [FinalResult.NUMERIC[c.final.result] for s in semesters for c in s.courses
               if c.final and c.final.result in FinalResult.NUMERIC]
    gpa = round(sum(numeric) / len(numeric), 2) if numeric else None
    debts = [c for s in semesters for c in s.courses if c.debt]

    student = await db.get(Student, student_id)
    next_exam = await _next_exam(db, student, today) if student and student.study_group_id else None

    recent = list((await db.scalars(
        select(GradeEvent).where(GradeEvent.student_id == student_id)
        .order_by(GradeEvent.created_at.desc(), GradeEvent.id.desc()).limit(events)
    )).all())
    return RecordBook(current=current, past=past, gpa=gpa, debts=debts, next_exam=next_exam, events=recent)


async def recent_teacher_activity(db: AsyncSession, teacher_id: int, limit: int = 6) -> list[tuple[datetime, str]]:
    """Последние выставленные этим преподавателем баллы — для главной кабинета."""
    rows = (await db.execute(
        select(Score.graded_at, Student.full_name, Assessment.title, Score.points, Assessment.max_points)
        .join(Assessment, Assessment.id == Score.assessment_id)
        .join(Course, and_(Course.id == Assessment.course_id, Course.teacher_id == teacher_id))
        .join(Student, Student.id == Score.student_id)
        .where(Score.graded_by == teacher_id)
        .order_by(Score.graded_at.desc()).limit(limit)
    )).all()
    return [(at, f"{name} — «{title}»: {scale.fmt_points(p)} из {scale.fmt_points(m)}") for at, name, title, p, m in rows]
