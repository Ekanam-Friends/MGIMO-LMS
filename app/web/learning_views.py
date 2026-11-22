"""Экраны заданий и посещаемости с серверной проверкой доступа."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import or_, select
from sqlalchemy.orm import undefer
from starlette.datastructures import UploadFile

from app import learning
from app.auth.service import csrf_ok, utcnow
from app.models import CourseEnrollment, Score, Student, User
from app.models.learning import Attendance, AttendanceWindow, Submission, TaskMessage
from app.web.deps import Viewer, check_csrf, current_viewer, get_db
from app.web.views import render

router = APIRouter()


async def upload_csrf(request: Request, viewer: Viewer = Depends(current_viewer)):
    try:
        form = await request.form(max_files=1, max_fields=5)
        given = request.headers.get("X-CSRF-Token") or form.get("csrf")
        if not csrf_ok(viewer.csrf, given if isinstance(given, str) else None):
            raise HTTPException(403, "CSRF")
        yield viewer
    finally:
        await request.close()


@router.get("/learning/tasks")
async def task_list(request: Request, viewer=Depends(current_viewer), db=Depends(get_db)):
    return render(request, "learning/tasks.html", viewer, tasks=await learning.tasks(db, viewer.user))


@router.get("/learning/tasks/{task_id}")
async def task_detail(request: Request, task_id: int, viewer=Depends(current_viewer), db=Depends(get_db)):
    a, course, discipline = await learning.task(db, viewer.user, task_id)
    teacher = viewer.user.role == "teacher"
    roster = (await db.scalars(select(Student).join(CourseEnrollment).where(
        CourseEnrollment.course_id == course.id,
        *([] if teacher else [Student.id == viewer.student_id])).order_by(Student.full_name))).all()
    ids = [s.id for s in roster]
    submissions = {s.student_id: s for s in (await db.scalars(select(Submission).where(
        Submission.assessment_id == task_id, Submission.student_id.in_(ids)))).all()}
    scores = {s.student_id: s for s in (await db.scalars(select(Score).where(
        Score.assessment_id == task_id, Score.student_id.in_(ids)))).all()}
    q = select(TaskMessage, User.role).join(User, User.id == TaskMessage.author_id).where(TaskMessage.assessment_id == task_id)
    if not teacher:
        q = q.where(or_(TaskMessage.student_id.is_(None), TaskMessage.student_id == viewer.student_id))
    messages = (await db.execute(q.order_by(TaskMessage.id))).all()
    response = render(request, "learning/task.html", viewer, task=a, course=course, discipline=discipline,
        roster=roster, submissions=submissions, scores=scores, messages=messages, statuses=learning.STATUSES,
        teacher=teacher)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/learning/tasks/{task_id}/submit")
async def submit(request: Request, task_id: int, viewer=Depends(upload_csrf), db=Depends(get_db)):
    await learning.task(db, viewer.user, task_id)
    if viewer.user.role != "student":
        raise learning.Missing()
    form = await request.form()
    upload = form.get("file")
    data, filename = None, ""
    if isinstance(upload, UploadFile) and upload.filename:
        data, filename = await upload.read(learning.MAX_FILE + 1), upload.filename
    text = form.get("text", "")
    if not isinstance(text, str):
        raise learning.Invalid("Текст решения должен быть строкой")
    await learning.submit(db, viewer.user, task_id, text, filename, data)
    await db.commit()
    return RedirectResponse(f"/learning/tasks/{task_id}", 303)


@router.get("/learning/tasks/{task_id}/files/{student_id}")
async def download(task_id: int, student_id: int, viewer=Depends(current_viewer), db=Depends(get_db)):
    _, course, _ = await learning.task(db, viewer.user, task_id)
    if viewer.user.role == "student" and student_id != viewer.student_id:
        raise learning.Missing()
    await learning.enrolled(db, course.id, student_id)
    row = await db.scalar(select(Submission).options(undefer(Submission.data)).where(
        Submission.assessment_id == task_id, Submission.student_id == student_id))
    if row is None or not row.filename or row.data is None:
        raise learning.Missing()
    return Response(row.data, media_type="application/octet-stream", headers={
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row.filename, safe=""),
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/learning/tasks/{task_id}/messages")
async def message(task_id: int, body: str = Form(...), student_id: int | None = Form(None),
                  viewer=Depends(check_csrf), db=Depends(get_db)):
    await learning.message(db, viewer.user, task_id, body, student_id)
    await db.commit()
    return RedirectResponse(f"/learning/tasks/{task_id}", 303)


@router.post("/learning/tasks/{task_id}/review")
async def review(task_id: int, student_id: int = Form(...), revision: int = Form(...),
                 status: str = Form(...), points: str = Form(""), comment: str = Form(""),
                 viewer=Depends(check_csrf), db=Depends(get_db)):
    await learning.review(db, viewer.user, task_id, student_id, revision, status, points, comment)
    await db.commit()
    return RedirectResponse(f"/learning/tasks/{task_id}", 303)


@router.get("/attendance")
async def attendance(request: Request, viewer=Depends(current_viewer), db=Depends(get_db)):
    lessons = await learning.lessons(db, viewer.user)
    marks = {}
    if viewer.user.role == "student":
        marks = {m.lesson_id: m for m in (await db.scalars(select(Attendance).where(
            Attendance.student_id == viewer.student_id, Attendance.lesson_id.in_([l.id for l in lessons])))).all()}
    return render(request, "learning/attendance.html", viewer, lessons=lessons, marks=marks)


async def attendance_detail(request, lesson_id, viewer, db, *, code=None, expires=None):
    if viewer.user.role != "teacher":
        raise learning.Missing()
    lesson = await learning.own_lesson(db, viewer.user, lesson_id)
    from app.models import LessonGroup
    roster = (await db.scalars(select(Student).join(LessonGroup, LessonGroup.group_id == Student.study_group_id)
        .where(LessonGroup.lesson_id == lesson_id).order_by(Student.full_name))).all()
    from app.schedule.choices import elective_catalog, get_choices, selected_lesson
    eligible = []
    for student in roster:
        if selected_lesson(lesson.discipline, lesson.subgroup, await elective_catalog(db, student.study_group_id),
                           await get_choices(db, student.id)):
            eligible.append(student)
    marks = {m.student_id: m for m in (await db.scalars(select(Attendance).where(Attendance.lesson_id == lesson_id,
        Attendance.student_id.in_([s.id for s in eligible])))).all()}
    window = await db.get(AttendanceWindow, lesson_id)
    response = render(request, "learning/attendance_detail.html", viewer, lesson=lesson, roster=eligible,
        marks=marks, code=code, expires=expires, window=window, now=utcnow())
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/attendance/{lesson_id}")
async def attendance_teacher(request: Request, lesson_id: int, viewer=Depends(current_viewer), db=Depends(get_db)):
    return await attendance_detail(request, lesson_id, viewer, db)


@router.post("/attendance/{lesson_id}/open")
async def open_window(request: Request, lesson_id: int, code: str = Form(""), viewer=Depends(check_csrf), db=Depends(get_db)):
    code, expires = await learning.open_attendance(db, viewer.user, lesson_id, code)
    await db.commit()
    return await attendance_detail(request, lesson_id, viewer, db, code=code, expires=expires)


@router.post("/attendance/{lesson_id}/check-in")
async def check_in(lesson_id: int, code: str = Form(...), viewer=Depends(check_csrf), db=Depends(get_db)):
    try:
        await learning.check_in(db, viewer.user, lesson_id, code)
    except learning.Invalid:
        await db.commit()  # Неверный код также расходует попытку.
        raise
    await db.commit()
    return RedirectResponse("/attendance", 303)


@router.post("/attendance/{lesson_id}/correct")
async def correct(lesson_id: int, student_id: int = Form(...), present: str = Form(...), reason: str = Form(...),
                  viewer=Depends(check_csrf), db=Depends(get_db)):
    if present not in {"yes", "no"}:
        raise learning.Invalid("Выберите присутствие или отсутствие")
    await learning.correct_attendance(db, viewer.user, lesson_id, student_id, present == "yes", reason)
    await db.commit()
    return RedirectResponse(f"/attendance/{lesson_id}", 303)


class BodyTooLarge(Exception):
    pass


class LearningBodyLimit:
    """Ограничиваем multipart до его разбора, в том числе без Content-Length."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].startswith("/learning/"):
            return await self.app(scope, receive, send)
        size = 0

        async def limited():
            nonlocal size
            msg = await receive()
            size += len(msg.get("body", b""))
            if size > learning.MAX_FILE + 256000:
                raise BodyTooLarge()
            return msg
        try:
            await self.app(scope, limited, send)
        except BodyTooLarge:
            await HTMLResponse('<html lang="ru"><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<p>Концепт для конкурса — не официальный сервис.</p><h1>Загрузка слишком большая</h1>'
                '<p>Файл — до 10 МБ.</p><a href="/learning/tasks">К заданиям</a></html>', 413)(scope, receive, send)
