"""Учебные сценарии. Права и адресаты определяются запросами к БД."""
import hashlib
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select

from app.auth.service import utcnow
from app.grades import service as grades
from app.models import Assessment, Course, CourseEnrollment, Discipline, Lesson, LessonGroup, Student, User, lesson_teachers
from app.models.learning import Attendance, AttendanceAttempt, AttendanceChange, AttendanceWindow, LearningNotice, Submission, TaskMessage
from app.schedule.choices import elective_catalog, get_choices, selected_lesson

MAX_FILE = 10 * 1024 * 1024
STATUSES = {"submitted": "Ждёт проверки", "reviewed": "Проверено", "returned": "На доработке"}


class Missing(LookupError):
    pass


class Invalid(ValueError):
    pass


async def task(db, user, task_id, *, lock=False):
    q = select(Assessment, Course, Discipline.name).join(Course, Course.id == Assessment.course_id).join(Discipline)
    if user.role == "teacher" and user.teacher_id:
        q = q.where(Course.teacher_id == user.teacher_id)
    elif user.role == "student" and user.student_id:
        q = q.join(CourseEnrollment, CourseEnrollment.course_id == Course.id).where(CourseEnrollment.student_id == user.student_id)
    else:
        raise Missing()
    q = q.where(Assessment.id == task_id, Assessment.kind == "homework")
    if lock:
        q = q.with_for_update(of=Assessment)
    row = (await db.execute(q)).first()
    if row is None:
        raise Missing()
    return row


async def tasks(db, user):
    q = select(Assessment, Course, Discipline.name).join(Course, Course.id == Assessment.course_id).join(Discipline)
    if user.role == "teacher" and user.teacher_id:
        q = q.where(Course.teacher_id == user.teacher_id)
    elif user.role == "student" and user.student_id:
        q = q.join(CourseEnrollment, CourseEnrollment.course_id == Course.id).where(CourseEnrollment.student_id == user.student_id)
    else:
        raise Missing()
    return (await db.execute(q.where(Assessment.kind == "homework").order_by(Assessment.due_date, Assessment.id))).all()


async def enrolled(db, course_id, student_id):
    if not await db.scalar(select(CourseEnrollment.student_id).where(
        CourseEnrollment.course_id == course_id, CourseEnrollment.student_id == student_id)):
        raise Missing()


async def notify(db, users, title, body, href):
    for uid in set(users):
        db.add(LearningNotice(user_id=uid, title=title[:300], body=body[:5000], href=href, created_at=utcnow()))


async def student_users(db, course_id, student_id=None):
    q = select(User.id).join(CourseEnrollment, CourseEnrollment.student_id == User.student_id).where(
        CourseEnrollment.course_id == course_id, User.role == "student")
    if student_id is not None:
        q = q.where(User.student_id == student_id)
    return (await db.scalars(q)).all()


async def submit(db, user, task_id, text, filename="", data=None):
    a, course, _ = await task(db, user, task_id, lock=True)
    if user.role != "student":
        raise Missing()
    if course.status == "closed":
        raise Invalid("Ведомость закрыта — сдача недоступна")
    text = text.strip()
    if not text and not data:
        raise Invalid("Добавьте текст решения или файл")
    if len(text) > 20000 or (data is not None and len(data) > MAX_FILE):
        raise Invalid("Текст — до 20 000 символов, файл — до 10 МБ")
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = "".join(c for c in filename if c.isprintable())[:200]
    row = await db.get(Submission, (task_id, user.student_id))
    if row is None:
        row = Submission(assessment_id=task_id, student_id=user.student_id, revision=0)
        db.add(row)
    row.revision += 1
    row.text, row.filename, row.data = text, filename if data else "", data
    row.status, row.updated_at = "submitted", utcnow()
    targets = (await db.scalars(select(User.id).where(User.role == "teacher", User.teacher_id == course.teacher_id))).all()
    await notify(db, targets, f"Сдано решение: {a.title}", "Студент отправил новую версию решения.", f"/learning/tasks/{task_id}")
    return row


async def message(db, user, task_id, body, student_id=None):
    a, course, _ = await task(db, user, task_id, lock=True)
    body = body.strip()
    if not body or len(body) > 5000:
        raise Invalid("Сообщение — от 1 до 5000 символов")
    if user.role == "student":
        student_id = user.student_id
        targets = (await db.scalars(select(User.id).where(User.role == "teacher", User.teacher_id == course.teacher_id))).all()
    else:
        if student_id is not None:
            await enrolled(db, course.id, student_id)
        targets = await student_users(db, course.id, student_id)
    db.add(TaskMessage(assessment_id=task_id, student_id=student_id, author_id=user.id, body=body, created_at=utcnow()))
    await notify(db, targets, f"Сообщение по заданию: {a.title}", body, f"/learning/tasks/{task_id}")


async def review(db, user, task_id, student_id, revision, status, points, comment):
    a, course, _ = await task(db, user, task_id, lock=True)
    if user.role != "teacher":
        raise Missing()
    await enrolled(db, course.id, student_id)
    row = await db.get(Submission, (task_id, student_id))
    if row is None:
        raise Missing()
    if row.revision != revision:
        raise Invalid("Студент уже отправил новую версию. Обновите страницу перед проверкой")
    if status not in {"reviewed", "returned"}:
        raise Invalid("Неизвестный результат проверки")
    if status == "reviewed" and not points.strip():
        raise Invalid("Укажите баллы за решение")
    if len(comment) > 500:
        raise Invalid("Комментарий к оценке — до 500 символов")
    if status == "returned" and not comment.strip():
        raise Invalid("Укажите, что необходимо доработать")
    try:
        # Баллы и комментарии живут в существующей ведомости, включая возврат без оценки.
        await grades.set_score(db, user.teacher_id, course.id, task_id, student_id,
                               points if status == "reviewed" else "", comment=comment.strip())
    except grades.ValidationError as exc:
        raise Invalid(str(exc)) from exc
    row.status = status
    if comment.strip():
        db.add(TaskMessage(assessment_id=task_id, student_id=student_id, author_id=user.id, body=comment.strip(), created_at=utcnow()))
    await notify(db, await student_users(db, course.id, student_id),
                 f"{STATUSES[status]}: {a.title}", comment, f"/learning/tasks/{task_id}")


async def lessons(db, user, *, now=None):
    now = now or utcnow()
    day = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Moscow")).date()
    q = select(Lesson).where(Lesson.date == day, Lesson.status == "active")
    if user.role == "teacher" and user.teacher_id:
        q = q.join(lesson_teachers).where(lesson_teachers.c.teacher_id == user.teacher_id)
    elif user.role == "student" and user.student_id:
        q = q.join(LessonGroup).join(Student, Student.study_group_id == LessonGroup.group_id).where(Student.id == user.student_id)
    else:
        raise Missing()
    rows = list((await db.scalars(q.order_by(Lesson.begins_at))).unique())
    if user.role == "student":
        student = await db.get(Student, user.student_id)
        catalog, choices = await elective_catalog(db, student.study_group_id), await get_choices(db, student.id)
        rows = [r for r in rows if selected_lesson(r.discipline, r.subgroup, catalog, choices)]
    return rows


async def own_lesson(db, user, lesson_id, *, lock=False, current=False, now=None):
    now = now or utcnow()
    q = select(Lesson).where(Lesson.id == lesson_id)
    if user.role == "teacher" and user.teacher_id:
        q = q.join(lesson_teachers).where(lesson_teachers.c.teacher_id == user.teacher_id)
    elif user.role == "student" and user.student_id:
        q = q.join(LessonGroup).join(Student, Student.study_group_id == LessonGroup.group_id).where(Student.id == user.student_id)
    else:
        raise Missing()
    if lock:
        q = q.with_for_update(of=Lesson)
    row = await db.scalar(q)
    if row is None:
        raise Missing()
    if user.role == "student":
        student = await db.get(Student, user.student_id)
        if not selected_lesson(row.discipline, row.subgroup, await elective_catalog(db, student.study_group_id),
                               await get_choices(db, student.id)):
            raise Missing()
    if current:
        local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Moscow"))
        if row.status != "active" or row.date != local.date() or not row.begins_at <= local.time().replace(tzinfo=None) < row.ends_at:
            raise Invalid("Отметка доступна только во время текущей пары")
    return row


def code_hash(salt, code):
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


async def open_attendance(db, user, lesson_id, code="", *, now=None):
    if user.role != "teacher":
        raise Missing()
    now = now or utcnow()
    await own_lesson(db, user, lesson_id, lock=True, current=True, now=now)
    code = code.strip() or f"{secrets.randbelow(1000000):06d}"
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise Invalid("Введите ровно шесть цифр")
    row = await db.get(AttendanceWindow, lesson_id)
    if row is None:
        row = AttendanceWindow(lesson_id=lesson_id)
        db.add(row)
    row.teacher_id, row.salt = user.teacher_id, secrets.token_hex(16)
    row.code_hash, row.opened_at, row.expires_at = code_hash(row.salt, code), now, now + timedelta(seconds=90)
    return code, row.expires_at


async def check_in(db, user, lesson_id, code, *, now=None):
    if user.role != "student":
        raise Missing()
    now = now or utcnow()
    await own_lesson(db, user, lesson_id, lock=True, current=True, now=now)
    window = await db.get(AttendanceWindow, lesson_id)
    if window is None or not window.opened_at <= now < window.expires_at:
        raise Invalid("Время отметки истекло или преподаватель ещё не открыл отметку")
    attempt = await db.get(AttendanceAttempt, (lesson_id, user.student_id))
    if attempt is None:
        attempt = AttendanceAttempt(lesson_id=lesson_id, student_id=user.student_id, opened_at=window.opened_at, attempts=0)
        db.add(attempt)
    if attempt.opened_at != window.opened_at:
        attempt.opened_at, attempt.attempts = window.opened_at, 0
    if attempt.attempts >= 5:
        raise Invalid("Лимит попыток исчерпан. Обратитесь к преподавателю")
    attempt.attempts += 1
    if not secrets.compare_digest(window.code_hash, code_hash(window.salt, code.strip())):
        # Вызывающий сценарий сохраняет счётчик и при неверном коде.
        raise Invalid("Неверный код")
    row = await db.get(Attendance, (lesson_id, user.student_id))
    if row is not None and row.corrected_by is not None:
        raise Invalid("Отметку уже исправил преподаватель. Обратитесь к нему")
    if row is None:
        row = Attendance(lesson_id=lesson_id, student_id=user.student_id)
        db.add(row)
    elif row.present:
        return row
    row.present, row.updated_at = True, now
    db.add(AttendanceChange(lesson_id=lesson_id, student_id=user.student_id, actor_id=user.id,
                            present=True, reason="Самостоятельная отметка по коду", created_at=now))
    return row


async def correct_attendance(db, user, lesson_id, student_id, present, reason):
    if user.role != "teacher":
        raise Missing()
    lesson = await own_lesson(db, user, lesson_id, lock=True)
    student = await db.scalar(select(Student).join(LessonGroup, LessonGroup.group_id == Student.study_group_id).where(
        Student.id == student_id, LessonGroup.lesson_id == lesson.id))
    if student is None or not selected_lesson(lesson.discipline, lesson.subgroup,
        await elective_catalog(db, student.study_group_id), await get_choices(db, student_id)):
        raise Missing()
    if not reason.strip() or len(reason) > 500:
        raise Invalid("Укажите причину исправления (до 500 символов)")
    row = await db.get(Attendance, (lesson_id, student_id))
    if row is None:
        row = Attendance(lesson_id=lesson_id, student_id=student_id)
        db.add(row)
    row.present, row.corrected_by, row.reason, row.updated_at = present, user.teacher_id, reason.strip(), utcnow()
    db.add(AttendanceChange(lesson_id=lesson_id, student_id=student_id, actor_id=user.id,
        present=present, reason=reason.strip(), created_at=utcnow()))
    targets = (await db.scalars(select(User.id).where(User.role == "student", User.student_id == student_id))).all()
    await notify(db, targets, "Преподаватель исправил посещаемость",
        f"{lesson.discipline}: {'присутствовал' if present else 'отсутствовал'}. {reason.strip()}", "/attendance")
