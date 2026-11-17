"""Минимальные факты кабинета. student_id поступает только из серверной сессии."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.grades.service import record_book
from app.models import Assessment, Course, CourseEnrollment, Discipline, FinalGrade, Lesson, LessonGroup, Score, Student, lesson_teachers
from app.models.certificates import CertificateKind, CertificateRequest, PaperStatus, RequestStatus
from app.models.grades import FinalResult
from .evidence import bounded_facts


class SqlPersonalEvidenceReader:
    """Адаптер чтения: транзакция принадлежит вызывающему HTTP-сценарию."""
    def __init__(self, db: AsyncSession):
        self.db = db

    async def collect(self, student_id, scopes, today, start, end):
        return await collect(self.db, student_id, scopes, today, start, end)


class SqlTeachingEvidenceReader:
    """SQL-адаптер ограничивает контекст учётной записью преподавателя."""
    def __init__(self, db: AsyncSession, teacher_id: int):
        self.db, self.teacher_id = db, teacher_id

    async def collect(self, student_id, scopes, today, start, end):
        return await collect_teaching(self.db, self.teacher_id, student_id, scopes, today, start, end)


async def collect(db: AsyncSession, student_id: int, scopes: list[str], today: date,
                  start: date, end: date) -> list[dict]:
    facts = []

    def add(text, url, label):
        facts.append({"id": f"fact-{len(facts) + 1}", "text": text, "url": url, "label": label})

    if "grades" in scopes:
        book = await record_book(db, student_id, today, events=0)
        semesters = ([book.current] if book.current else []) + book.past
        for semester in semesters:
            for course in semester.courses:
                name = course.card.discipline
                final = (f"Итог: {FinalResult.TITLES.get(course.final.result, course.final.result)}, {course.final.total_points:g} баллов, "
                         f"дата {course.final.graded_on}." if course.final else "Итоговая оценка не выставлена.")
                add(f"{name}; {semester.title}. Накоплено: {course.current:g} баллов. {final}",
                    "/grades", "Успеваемость")
                for item in course.assessments:
                    if item.score:
                        add(f"{name}; {item.assessment.title}: {item.score.points:g} из "
                            f"{item.assessment.max_points:g} баллов; выставлено {item.score.graded_at.date()}.",
                            "/grades", "Успеваемость")
        if not facts:
            add("В кабинете нет записей об успеваемости текущего студента.", "/grades", "Успеваемость")
    if "schedule" in scopes:
        # Группа определяется в SQL через студента, а не передаётся пользователем или моделью.
        rows = (await db.scalars(select(Lesson).join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
            .join(Student, Student.study_group_id == LessonGroup.group_id)
            .where(Student.id == student_id, Lesson.date >= start, Lesson.date <= end)
            .order_by(Lesson.date, Lesson.begins_at, Lesson.id).limit(61))).all()
        for row in rows[:60]:
            add(f"{row.date} {row.begins_at:%H:%M}–{row.ends_at:%H:%M}: {row.discipline}; "
                f"{row.kind_of_work}; {row.auditorium or 'аудитория не указана'}; "
                f"{row.building or 'корпус не указан'}; подгруппа: {row.subgroup or 'вся группа'}; "
                f"статус: {row.status}. Последнее наблюдение при импорте: {row.last_seen_at.isoformat()}.",
                f"/schedule?week={row.date}", "Расписание группы")
        if not rows:
            add(f"В снимке расписания группы нет занятий за {start}–{end}. "
                "Это не подтверждает отсутствие занятий вне загруженного расписания.", "/schedule", "Расписание группы")
        if len(rows) > 60:
            add("Показаны первые 60 занятий периода; полный список доступен в расписании.", "/schedule", "Ограничение выборки")
    if "requests" in scopes:
        rows = (await db.scalars(select(CertificateRequest)
            .where(CertificateRequest.student_id == student_id)
            .order_by(CertificateRequest.created_at.desc(), CertificateRequest.id.desc()).limit(21))).all()
        for row in rows[:20]:
            add(f"Заявка №{row.id}: {CertificateKind.TITLES.get(row.kind, row.kind)}; "
                f"статус: {RequestStatus.TITLES.get(row.status, row.status)}; "
                f"бумажный оригинал: {PaperStatus.TITLES.get(row.paper_status, row.paper_status)}; "
                f"обновлено в кабинете {row.updated_at.isoformat()}; "
                f"последняя синхронизация с деканатом: {row.synced_at.isoformat() if row.synced_at else 'ещё не выполнена'}.",
                f"/mfc/certificates/{row.id}", "Заявки")
        if not rows:
            add("В кабинете нет заявок текущего студента.", "/mfc/certificates", "Заявки")
        if len(rows) > 20:
            add("Выбраны последние 20 заявок. Полная история доступна в разделе справок.", "/mfc/certificates", "Ограничение выборки")
    # Не отправляем провайдеру неограниченный журнал оценок.
    return bounded_facts(facts)


async def collect_teaching(db, teacher_id: int, student_id: int | None, scopes: list[str],
                           today: date, start: date, end: date) -> list[dict]:
    """Ни полная зачётка, ни заявки студента не читаются для преподавателя."""
    from .teaching import course_ids, coverage
    if teacher_id is None:
        raise ValueError("Не подтверждена учётная запись преподавателя")
    facts = []

    def add(text, label="Свои дисциплины"):
        facts.append({"id": f"fact-{len(facts) + 1}", "text": text, "url": "", "label": label})

    status = await coverage(db, teacher_id, student_id)
    add(f"Снимок доступных учебных данных: дисциплин {status['courses']}, контрольных точек "
        f"{status['assessments']}, выставленных баллов {status['scores']}. "
        f"Последнее наблюдение своих занятий при импорте: {status['schedule_imported_at'] or 'не подтверждено'}. "
        "Отсутствие записи не доказывает отсутствие события вне загруженного снимка.")
    enrolled = select(Course.id).join(CourseEnrollment).where(Course.id.in_(course_ids(teacher_id, today)))
    if student_id is not None:
        enrolled = enrolled.where(CourseEnrollment.student_id == student_id)
    if "grades" in scopes:
        rows = (await db.execute(select(Discipline.name, Course.semester, Assessment.title,
            Assessment.max_points, Score.points, Score.graded_at, Score.student_id).select_from(Course)
            .join(Discipline).join(Assessment).outerjoin(Score,
                (Score.assessment_id == Assessment.id) &
                ((Score.student_id == student_id) if student_id is not None else Score.student_id.in_(
                    select(CourseEnrollment.student_id).where(CourseEnrollment.course_id == Course.id).correlate(Course))))
            .where(Course.id.in_(enrolled)).order_by(Course.id, Assessment.id).limit(100))).all()
        for name, sem, title, mx, points, graded_at, sid in rows:
            add((f"Студент №{sid}: " if sid is not None else "") + f"{name}, семестр {sem}; {title}: " +
                (f"{points:g} из {mx:g}; выставлено {graded_at.isoformat()}." if points is not None
                 else "баллы в снимке не выставлены."))
        finals_query = select(Discipline.name, FinalGrade.result, FinalGrade.total_points,
            FinalGrade.graded_on, FinalGrade.student_id).select_from(FinalGrade).join(Course).join(Discipline)
        finals_query = finals_query.where(Course.id.in_(enrolled), FinalGrade.student_id.in_(
            select(CourseEnrollment.student_id).where(CourseEnrollment.course_id == Course.id).correlate(Course)))
        if student_id is not None:
            finals_query = finals_query.where(FinalGrade.student_id == student_id)
        finals = (await db.execute(finals_query.order_by(FinalGrade.graded_on.desc(), FinalGrade.id.desc()).limit(40))).all()
        for name, result, points, day, sid in finals:
            add(f"Студент №{sid}: {name}: итог {FinalResult.TITLES.get(result, result)}, {points:g} баллов, дата {day}.")
        if len(rows) == 100 or len(finals) == 40:
            add("Выборка ограничена; уточните дисциплину и период. Полная история не подтверждена.", "Ограничение выборки")
    if "schedule" in scopes:
        schedule_query = select(Lesson).join(lesson_teachers, lesson_teachers.c.lesson_id == Lesson.id)
        if student_id is not None:
            schedule_query = schedule_query.join(LessonGroup).join(Student, Student.study_group_id == LessonGroup.group_id)
            schedule_query = schedule_query.where(Student.id == student_id)
        rows = (await db.scalars(schedule_query.where(lesson_teachers.c.teacher_id == teacher_id,
            Lesson.date >= start, Lesson.date <= end, Lesson.status == "active")
            .order_by(Lesson.date, Lesson.begins_at, Lesson.id).limit(61))).all()
        for row in rows[:60]:
            add(f"{row.date} {row.begins_at:%H:%M}–{row.ends_at:%H:%M}: {row.discipline}; "
                f"подгруппа {row.subgroup or 'вся группа'}; импорт {row.last_seen_at.isoformat()}.")
        if not rows:
            add(f"В загруженном снимке нет своих занятий этой группы за {start}–{end}.")
        if len(rows) > 60:
            add("Показаны первые 60 занятий. Уточните период.", "Ограничение выборки")
    if "requests" in scopes:
        add("Заявки и справки студента не входят в область доступа преподавателя.")
    return bounded_facts(facts)
