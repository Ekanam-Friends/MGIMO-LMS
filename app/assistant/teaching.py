"""Серверная область преподавателя: свои дисциплины, зачисления и снимок данных."""
from datetime import date

from sqlalchemy import and_, exists, func, or_, select

from app.grades.scale import academic_year
from app.models import Assessment, Course, CourseEnrollment, Discipline, Lesson, LessonGroup, Score, Student, lesson_teachers


def course_ids(teacher_id: int, today: date | None = None):
    # У дисциплины может быть несколько преподавателей в РУЗ, но только один
    # ведущий ведомости. Остальным доступно чтение этой дисциплины у своих групп.
    imported = exists(select(Lesson.id).join(LessonGroup, LessonGroup.lesson_id == Lesson.id)
        .join(lesson_teachers, lesson_teachers.c.lesson_id == Lesson.id)
        .where(LessonGroup.group_id == Course.group_id, Lesson.discipline == Discipline.name,
               lesson_teachers.c.teacher_id == teacher_id, Lesson.status == "active",
               Lesson.date >= date(int(academic_year(today or date.today())[:4]), 9, 1),
               Lesson.date < date(int(academic_year(today or date.today())[:4]) + 1, 9, 1)))
    return select(Course.id).join(Discipline, Discipline.id == Course.discipline_id).where(or_(
        Course.teacher_id == teacher_id,
        and_(Course.academic_year == academic_year(today or date.today()), imported)))


def student_ids(teacher_id: int):
    return select(CourseEnrollment.student_id).where(CourseEnrollment.course_id.in_(course_ids(teacher_id)))


async def coverage(db, teacher_id: int, student_id: int | None = None) -> dict:
    allowed = course_ids(teacher_id)
    enrolled = select(CourseEnrollment.course_id).where(CourseEnrollment.course_id.in_(allowed))
    if student_id is not None:
        enrolled = enrolled.where(CourseEnrollment.student_id == student_id)
    courses = await db.scalar(select(func.count()).select_from(Course).where(Course.id.in_(enrolled)))
    assessments = await db.scalar(select(func.count()).select_from(Assessment).where(Assessment.course_id.in_(enrolled)))
    scores_query = select(func.count()).select_from(Score).join(Assessment).where(Assessment.course_id.in_(enrolled))
    if student_id is not None:
        scores_query = scores_query.where(Score.student_id == student_id)
    scores = await db.scalar(scores_query)
    imported_query = select(func.max(Lesson.last_seen_at)).join(lesson_teachers,
        lesson_teachers.c.lesson_id == Lesson.id).where(lesson_teachers.c.teacher_id == teacher_id,
                                                       Lesson.status == "active")
    if student_id is not None:
        imported_query = imported_query.join(LessonGroup).join(Student, Student.study_group_id == LessonGroup.group_id)
        imported_query = imported_query.where(Student.id == student_id)
    imported_at = await db.scalar(imported_query)
    return {"courses": courses, "assessments": assessments, "scores": scores,
            "schedule_imported_at": imported_at.isoformat() if imported_at else None}
