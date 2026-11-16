"""Минимальные факты кабинета. student_id поступает только из серверной сессии."""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.grades.service import record_book
from app.models import Lesson, LessonGroup, Student
from app.models.certificates import CertificateKind, CertificateRequest, PaperStatus, RequestStatus
from app.models.grades import FinalResult
from .evidence import bounded_facts


class SqlPersonalEvidenceReader:
    """Адаптер чтения: транзакция принадлежит вызывающему HTTP-сценарию."""
    def __init__(self, db: AsyncSession):
        self.db = db

    async def collect(self, student_id, scopes, today, start, end):
        return await collect(self.db, student_id, scopes, today, start, end)


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
