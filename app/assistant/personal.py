"""Минимальные факты кабинета. student_id поступает только из серверной сессии."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import re

from sqlalchemy import select, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.grades.service import record_book
from app.models import Assessment, Course, CourseEnrollment, Discipline, FinalGrade, Lesson, LessonGroup, Score, Student, Teacher, lesson_teachers
from app.models.certificates import CertificateKind, CertificateRequest, PaperStatus, RequestStatus
from app.models.grades import FinalResult, AssessmentKind
from app.models.schedule import LessonStatus
from app.schedule.choices import elective_catalog, get_choices, selected_lesson, subgroup_parts
from .evidence import bounded_facts


class SqlPersonalEvidenceReader:
    """Адаптер чтения: транзакция принадлежит вызывающему HTTP-сценарию."""
    def __init__(self, db: AsyncSession):
        self.db = db

    async def collect(self, student_id, scopes, today, start, end, *, question=""):
        return await collect(self.db, student_id, scopes, today, start, end, question=question)


class SqlTeachingEvidenceReader:
    """SQL-адаптер ограничивает контекст учётной записью преподавателя."""
    def __init__(self, db: AsyncSession, teacher_id: int):
        self.db, self.teacher_id = db, teacher_id

    async def collect(self, student_id, scopes, today, start, end, *, question=""):
        return await collect_teaching(self.db, self.teacher_id, student_id, scopes, today, start, end)


async def collect(db: AsyncSession, student_id: int, scopes: list[str], today: date,
                  start: date, end: date, *, question: str = "") -> list[dict]:
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
    if "homework" in scopes:
        rows = (await db.execute(select(Assessment, Discipline.name).select_from(Assessment)
            .join(Course, Course.id == Assessment.course_id).join(Discipline, Discipline.id == Course.discipline_id)
            .join(CourseEnrollment, CourseEnrollment.course_id == Course.id)
            .where(CourseEnrollment.student_id == student_id, Assessment.kind == AssessmentKind.HOMEWORK)
            .order_by(case((Assessment.due_date >= today, 0), (Assessment.due_date.is_(None), 1), else_=2),
                      case((Assessment.due_date >= today, Assessment.due_date)), Assessment.due_date.desc(),
                      Assessment.id.desc()).limit(61))).all()
        for task, discipline in rows[:60]:
            due = task.due_date.isoformat() if task.due_date else "не указан"
            add(f"{discipline}: {task.title}. Срок: {due}. {task.description[:400]} "
                "Сведения о сдаче и проверке в этой выборке не запрашивались.", "/grades", "Домашние задания")
        if not rows:
            add("В загруженных курсах текущего студента нет записей домашних заданий; "
                "это не подтверждает отсутствие заданий вне кабинета.", "/grades", "Домашние задания")
        if len(rows) > 60 or any(len(task.description) > 400 for task, _ in rows[:60]):
            add("Список заданий или длинные описания сокращены; сначала показаны ближайшие сроки. "
                "Полные тексты и история доступны в успеваемости.", "/grades", "Ограничение выборки")
    if "schedule" in scopes:
        q = question.lower().replace("ё", "е")
        # Для вопроса без явной даты не предлагаем уже закончившиеся пары.
        nearest = bool(re.search(r"когда|ближайш|следующ", q)) and not re.search(
            r"сегодня|завтра|вчера|недел|понедельник|вторник|сред[ау]|четверг|пятниц|суббот|воскресень|\d", q)
        if nearest:
            now = datetime.now(ZoneInfo("Europe/Moscow"))
            start, end = now.date(), now.date() + timedelta(days=13)
        query = (select(Lesson).join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
            .join(Student, Student.study_group_id == LessonGroup.group_id)
            .where(Student.id == student_id, Lesson.date >= start, Lesson.date <= end))
        if nearest:
            query = query.where(Lesson.status == LessonStatus.ACTIVE,
                or_(Lesson.date > now.date(), (Lesson.date == now.date()) & (Lesson.ends_at > now.time().replace(tzinfo=None))))
        # Имена языковых дисциплин отбираются до лимита, без выдуманных имён преподавателей.
        for needle in ("английск", "немецк", "французск", "испанск", "китайск"):
            if needle in q:
                query = query.where(Lesson.discipline.ilike(f"%{needle}%"))
        student = await db.get(Student, student_id)
        catalog = await elective_catalog(db, student.study_group_id if student else None)
        choices = await get_choices(db, student_id)
        query = query.order_by(Lesson.date, Lesson.begins_at, Lesson.id)
        rows, ambiguous = [], False
        # Фильтруем выбор до лимита фактов; чужая группа исключена SQL-запросом.
        stream = await db.stream_scalars(query)
        try:
            async for row in stream:
                if not selected_lesson(row.discipline, row.subgroup, catalog, choices):
                    parts = subgroup_parts(row.subgroup)
                    alternatives = ({name for name, disciplines in catalog.subgroups.items()
                                     if row.discipline in disciplines} if parts else
                                    set(next((family for family in catalog.families if row.discipline in family), [])))
                    selected = choices[0] if parts else choices[1]
                    ambiguous = ambiguous or not bool(alternatives & selected)
                    continue
                if nearest and rows and (row.date, row.begins_at) != (rows[0].date, rows[0].begins_at):
                    break
                rows.append(row)
                if len(rows) >= 61:
                    break
        finally:
            await stream.close()
        teachers = {}
        if rows:
            for lid, name in (await db.execute(select(lesson_teachers.c.lesson_id, Teacher.full_name)
                .join(Teacher, Teacher.id == lesson_teachers.c.teacher_id)
                .where(lesson_teachers.c.lesson_id.in_([r.id for r in rows[:60]])))).all():
                teachers.setdefault(lid, []).append(name)
        for row in rows[:60]:
            status = "занятие отменено" if row.status != LessonStatus.ACTIVE else "занятие по расписанию"
            add(f"{row.date} {row.begins_at:%H:%M}–{row.ends_at:%H:%M}: {row.discipline}; "
                f"{row.kind_of_work}; {row.auditorium or 'аудитория не указана'}; "
                f"{row.building or 'корпус не указан'}; подгруппа: {row.subgroup or 'вся группа'}; "
                f"преподаватель: {', '.join(teachers.get(row.id, [])) or 'в снимке не указан'}; {status}. "
                f"Снимок расписания: {row.last_seen_at:%d.%m.%Y %H:%M}.",
                f"/schedule?week={row.date}", "Расписание студента")
        if ambiguous:
            add("Часть языковых подгрупп или элективов не включена: проверьте свой выбор "
                "в календаре. Если нужная подгруппа не выбрана, уточните её в разделе элективов.",
                "/electives", "Ограничение выборки")
        if not rows:
            add(f"В выбранном расписании нет подходящих занятий за {start}–{end}. "
                "Это не подтверждает отсутствие занятий вне загруженного снимка.", "/schedule", "Расписание студента")
        if len(rows) > 60:
            add("Показаны первые 60 занятий периода; уточните дисциплину или период.", "/schedule", "Ограничение выборки")
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
    if "homework" in scopes:
        tasks = (await db.execute(select(Discipline.name, Assessment.title, Assessment.description, Assessment.due_date)
            .select_from(Assessment).join(Course).join(Discipline)
            .where(Course.id.in_(enrolled), Assessment.kind == AssessmentKind.HOMEWORK)
            .order_by(Assessment.due_date.desc(), Assessment.id.desc()).limit(61))).all()
        for name, title, description, due in tasks[:60]:
            add(f"{name}: {title}. Срок: {due or 'не указан'}. {description[:400]}", "Домашние задания")
        if len(tasks) > 60 or any(len(row[2]) > 400 for row in tasks[:60]):
            add("Список заданий или длинные описания сокращены; уточните дисциплину.", "Ограничение выборки")
        if not tasks:
            add("В доступных своих дисциплинах нет записей домашних заданий.", "Домашние задания")
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
