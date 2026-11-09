"""Кабинет преподавателя (решение docs/decisions/0006-grades.md): курсы, журнал баллов,
домашние задания, итоговая ведомость, расписание.

Все роуты — под /t и только для учёток с ролью teacher. id преподавателя берётся из сессии
(`viewer.teacher_id`); курс, задание и студент в адресе проверяются запросом в app/grades/service.py:
чужое → 404.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.grades import scale
from app.grades import service as grades
from app.models.grades import AssessmentKind, ControlForm, FinalResult
from app.web import schedule_view as sv
from app.web.deps import Viewer, check_csrf, current_viewer, get_db
from app.web.views import _parse_day, render, templates, today

router = APIRouter(prefix="/t")

templates.env.globals.update(scale=scale, ControlForm=ControlForm, FinalResult=FinalResult,
                             AssessmentKind=AssessmentKind)
templates.env.filters["pts"] = scale.fmt_points


async def teacher_viewer(viewer: Viewer = Depends(current_viewer)) -> Viewer:
    if viewer.teacher is None:
        raise HTTPException(status_code=404)
    return viewer


async def teacher_csrf(viewer: Viewer = Depends(check_csrf)) -> Viewer:
    if viewer.teacher is None:
        raise HTTPException(status_code=404)
    return viewer


def _is_htmx(request: Request) -> bool:
    return bool(request.headers.get("HX-Request"))


async def _journal_or_404(db: AsyncSession, viewer: Viewer, course_id: int) -> grades.Journal:
    j = await grades.journal(db, viewer.teacher_id, course_id)
    if j is None:
        raise HTTPException(status_code=404)
    return j


# --- курсы ---


@router.get("", include_in_schema=False)
async def teacher_root() -> Response:
    return RedirectResponse("/t/courses", status_code=303)


@router.get("/courses", response_class=HTMLResponse)
async def courses(request: Request, year: str | None = None, viewer: Viewer = Depends(teacher_viewer),
                  db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    current_year = scale.academic_year(today())
    all_cards = await grades.teacher_courses(db, viewer.teacher_id)
    years = sorted({c.course.academic_year for c in all_cards}, reverse=True) or [current_year]
    shown = year if year in years else (current_year if current_year in years else years[0])
    cards = [c for c in all_cards if c.course.academic_year == shown]
    due_soon = [c for c in cards if c.next_due and c.next_due <= today() + timedelta(days=7)]
    activity = await grades.recent_teacher_activity(db, viewer.teacher_id)
    return render(request, "teacher/courses.html", viewer, cards=cards, years=years, shown=shown,
                  current_year=current_year, due_soon=due_soon, activity=activity,
                  students_total=sum(c.students for c in cards))


@router.get("/courses/{course_id}", response_class=HTMLResponse)
async def course_journal(request: Request, course_id: int, viewer: Viewer = Depends(teacher_viewer),
                         db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    j = await _journal_or_404(db, viewer, course_id)
    return render(request, "teacher/journal.html", viewer, j=j)


@router.post("/courses/{course_id}/scores", response_class=HTMLResponse)
async def score_save(
    request: Request, course_id: int, assessment_id: int = Form(...), student_id: int = Form(...),
    points: str = Form(""), comment: str = Form(""), back: str = Form(""),
    viewer: Viewer = Depends(teacher_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    assessment = await grades.get_assessment(db, viewer.teacher_id, course_id, assessment_id)
    if assessment is None:
        raise HTTPException(status_code=404)
    error = None
    try:
        found = await grades.set_score(db, viewer.teacher_id, course_id, assessment_id, student_id, points, comment)
    except grades.ValidationError as e:
        found, error = None, str(e)
    else:
        if found is None:
            raise HTTPException(status_code=404)
        await db.commit()
    if not _is_htmx(request):
        # Только свои страницы: произвольный адрес из формы — открытый редирект.
        target = back if back.startswith(f"/t/courses/{course_id}") else f"/t/courses/{course_id}"
        return RedirectResponse(target, status_code=303)
    score, current = found if found else (None, None)
    if error is not None:
        current = (await grades.current_points(db, course_id, [student_id])).get(student_id, 0.0)
    return render(request, "teacher/_score_cell.html", viewer, a=assessment, student_id=student_id,
                  course_id=course_id, score=score, raw=points if error else None, error=error,
                  current=current, saved=error is None, oob=True)


# --- задания ---


@router.get("/courses/{course_id}/assessments/new", response_class=HTMLResponse)
async def assessment_new(request: Request, course_id: int, kind: str = AssessmentKind.HOMEWORK,
                         viewer: Viewer = Depends(teacher_viewer), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    j = await _journal_or_404(db, viewer, course_id)
    form = {"kind": kind if kind in AssessmentKind.TITLES else AssessmentKind.HOMEWORK, "title": "",
            "description": "", "max_points": "10", "due_date": (today() + timedelta(days=7)).isoformat()}
    return render(request, "teacher/assessment_form.html", viewer, j=j, form=form, error=None)


@router.post("/courses/{course_id}/assessments")
async def assessment_create(
    request: Request, course_id: int, kind: str = Form(...), title: str = Form(""), description: str = Form(""),
    max_points: str = Form("10"), due_date: str = Form(""),
    viewer: Viewer = Depends(teacher_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        a = await grades.create_assessment(db, viewer.teacher_id, course_id, kind=kind, title=title,
                                           description=description, max_points=max_points, due_date=due_date)
    except grades.ValidationError as e:
        j = await _journal_or_404(db, viewer, course_id)
        form = {"kind": kind, "title": title, "description": description, "max_points": max_points, "due_date": due_date}
        resp = render(request, "teacher/assessment_form.html", viewer, j=j, form=form, error=str(e))
        resp.status_code = 400
        return resp
    if a is None:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse(f"/t/courses/{course_id}/assessments/{a.id}?created=1", status_code=303)


@router.get("/courses/{course_id}/assessments/{assessment_id}", response_class=HTMLResponse)
async def assessment_grade(request: Request, course_id: int, assessment_id: int, created: int = 0,
                           viewer: Viewer = Depends(teacher_viewer), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    j = await _journal_or_404(db, viewer, course_id)
    a = next((x for x in j.assessments if x.id == assessment_id), None)
    if a is None:
        raise HTTPException(status_code=404)
    graded = sum(1 for r in j.rows if a.id in r.scores)
    return render(request, "teacher/assessment.html", viewer, j=j, a=a, graded=graded, created=created)


@router.post("/courses/{course_id}/assessments/{assessment_id}/delete")
async def assessment_delete(course_id: int, assessment_id: int, viewer: Viewer = Depends(teacher_csrf),
                            db: AsyncSession = Depends(get_db)) -> Response:
    try:
        ok = await grades.delete_assessment(db, viewer.teacher_id, course_id, assessment_id)
    except grades.Locked:
        raise HTTPException(status_code=409)
    if not ok:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse(f"/t/courses/{course_id}", status_code=303)


@router.get("/homework", response_class=HTMLResponse)
async def homework(request: Request, viewer: Viewer = Depends(teacher_viewer),
                   db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    items = await grades.teacher_homework(db, viewer.teacher_id, scale.academic_year(today()))
    t = today()
    upcoming = sorted([i for i in items if i.assessment.due_date and i.assessment.due_date >= t],
                      key=lambda i: i.assessment.due_date)
    to_check = [i for i in items if i.assessment.due_date and i.assessment.due_date < t and i.submitted < i.students]
    done = [i for i in items if i not in upcoming and i not in to_check]
    return render(request, "teacher/homework.html", viewer, upcoming=upcoming, to_check=to_check, done=done)


# --- итоговая ведомость ---


@router.get("/courses/{course_id}/final", response_class=HTMLResponse)
async def final_sheet(request: Request, course_id: int, error: str | None = None,
                      viewer: Viewer = Depends(teacher_viewer), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    j = await _journal_or_404(db, viewer, course_id)
    return render(request, "teacher/final.html", viewer, j=j, error=error)


@router.post("/courses/{course_id}/final", response_class=HTMLResponse)
async def final_save(
    request: Request, course_id: int, student_id: int = Form(...), result: str = Form(...),
    exam_points: str = Form(""), viewer: Viewer = Depends(teacher_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    error = None
    try:
        fg = await grades.set_final(db, viewer.teacher_id, course_id, student_id, result=result,
                                    exam_points=exam_points or None)
    except grades.ValidationError as e:
        error = str(e)
    else:
        if fg is None:
            raise HTTPException(status_code=404)
        await db.commit()
    if not _is_htmx(request):
        return RedirectResponse(f"/t/courses/{course_id}/final", status_code=303)
    j = await _journal_or_404(db, viewer, course_id)
    row = next((r for r in j.rows if r.student.id == student_id), None)
    if row is None:
        raise HTTPException(status_code=404)
    return render(request, "teacher/_final_row.html", viewer, j=j, r=row, error=error, saved=error is None)


@router.post("/courses/{course_id}/close")
async def course_close(request: Request, course_id: int, viewer: Viewer = Depends(teacher_csrf),
                       db: AsyncSession = Depends(get_db)) -> Response:
    try:
        course = await grades.close_course(db, viewer.teacher_id, course_id)
    except grades.ValidationError as e:
        j = await _journal_or_404(db, viewer, course_id)
        resp = render(request, "teacher/final.html", viewer, j=j, error=str(e))
        resp.status_code = 400
        return resp
    if course is None:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse(f"/t/courses/{course_id}/final", status_code=303)


# --- расписание преподавателя ---


@router.get("/schedule", response_class=HTMLResponse)
async def schedule(request: Request, week: str | None = None, viewer: Viewer = Depends(teacher_viewer),
                   db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    start = sv.week_start(_parse_day(week, today()))
    days = [start + timedelta(days=i) for i in range(7)]
    lessons = await sv.teacher_lessons_for(db, viewer.teacher_id, days[0], days[-1])
    return render(request, "teacher/schedule.html", viewer, days=days, lessons=lessons, today=today(),
                  prev_week=start - timedelta(days=7), next_week=start + timedelta(days=7))

