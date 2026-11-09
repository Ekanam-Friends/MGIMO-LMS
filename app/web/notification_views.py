"""Колокольчик в шапке и кабинет деканата (решение docs/decisions/0009-notifications.md).

* `/notifications/*` — лента, счётчик и отметки о прочтении (любой вошедший, данные по сессии);
* `/d/*` — кабинет сотрудника деканата: объявления от деканата или «от вуза»;
* `/t/announcements` — объявления преподавателя только своим группам.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import Role
from app.models import Student, StudyGroup
from app.models.notifications import Audience, Source
from app.notifications import service as notes
from app.web.deps import Viewer, check_csrf, current_viewer, get_db
from app.web.teacher_views import teacher_csrf, teacher_viewer
from app.web.views import render

router = APIRouter()
staff_router = APIRouter(prefix="/d")
teacher_router = APIRouter(prefix="/t")


# --- колокольчик ---

@router.get("/notifications/badge", response_class=HTMLResponse)
async def badge(request: Request, viewer: Viewer = Depends(current_viewer),
                db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    items = await notes.feed(db, viewer.user, viewer.student)
    return render(request, "partials/_bell_badge.html", viewer, count=sum(i.unread for i in items))


@router.get("/notifications/panel", response_class=HTMLResponse)
async def panel(request: Request, viewer: Viewer = Depends(current_viewer),
                db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return render(request, "partials/_bell_panel.html", viewer, items=await notes.feed(db, viewer.user, viewer.student))


@router.get("/notifications", response_class=HTMLResponse)
async def page(request: Request, viewer: Viewer = Depends(current_viewer),
               db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return render(request, "notifications.html", viewer, items=await notes.feed(db, viewer.user, viewer.student))


@router.post("/notifications/read/{kind}/{ref}")
async def read_one(kind: str, ref: int, viewer: Viewer = Depends(check_csrf),
                   db: AsyncSession = Depends(get_db)) -> Response:
    if kind not in notes.KINDS:
        raise HTTPException(status_code=404)
    await notes.mark_read(db, viewer.user.id, kind, ref)
    return Response(status_code=204, headers={"HX-Trigger": "notif-changed"})


@router.post("/notifications/read-all", response_class=HTMLResponse)
async def read_all(request: Request, viewer: Viewer = Depends(check_csrf),
                   db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    await notes.mark_all_read(db, viewer.user, viewer.student)
    resp = render(request, "partials/_bell_panel.html", viewer, items=await notes.feed(db, viewer.user, viewer.student))
    resp.headers["HX-Trigger"] = "notif-changed"
    return resp


# --- кабинет деканата ---

def _require_staff(viewer: Viewer) -> Viewer:
    if viewer.user.role != Role.STAFF:
        raise HTTPException(status_code=404)
    return viewer


async def staff_viewer(viewer: Viewer = Depends(current_viewer)) -> Viewer:
    return _require_staff(viewer)


async def staff_csrf(viewer: Viewer = Depends(check_csrf)) -> Viewer:
    return _require_staff(viewer)


async def _form_ctx(db: AsyncSession, viewer: Viewer, mode: str, **extra) -> dict:
    ctx = {"mode": mode, "mine": await notes.own(db, viewer.user.id), "Audience": Audience, "Source": Source,
           "error": None, "sent": False, "values": {}, **extra}
    if mode == "staff":
        ctx["faculties"] = await notes.faculty_names(db)
        ctx["programs"] = await notes.programs(db)
    else:
        ctx["groups"] = await notes.teacher_groups(db, viewer.teacher_id)
    return ctx


@staff_router.get("/announcements", response_class=HTMLResponse)
async def staff_form(request: Request, sent: int = 0, viewer: Viewer = Depends(staff_viewer),
                     db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return render(request, "announce.html", viewer, **await _form_ctx(db, viewer, "staff", sent=bool(sent)))


@staff_router.post("/announcements", response_class=HTMLResponse)
async def staff_send(
    request: Request, title: str = Form(""), body: str = Form(""), source: str = Form(Source.DEAN),
    audience: str = Form(Audience.ALL), faculty: str = Form(""), group: str = Form(""),
    program: str = Form(""), course: int = Form(0), student_id: int = Form(0),
    viewer: Viewer = Depends(staff_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    values = {"title": title, "body": body, "source": source, "audience": audience, "faculty": faculty,
              "group": group, "program": program, "course": course or None, "student_id": student_id}
    try:
        if source == Source.TEACHER:
            raise notes.AnnouncementError("Недопустимый отправитель.")
        grp = await notes.find_group(db, group) if audience == Audience.GROUP else None
        stu = await db.get(Student, student_id) if audience == Audience.STUDENT and student_id else None
        await notes.create(db, user=viewer.user, source=source, title=title, body=body, audience=audience,
                           faculty_name=faculty or None, group=grp, program=program or None,
                           course=course or None, student=stu)
    except notes.AnnouncementError as e:
        picked = await db.get(Student, student_id) if student_id else None
        resp = render(request, "announce.html", viewer,
                      **await _form_ctx(db, viewer, "staff", error=str(e), values=values, picked=picked))
        resp.status_code = 400
        return resp
    return RedirectResponse("/d/announcements?sent=1", status_code=303)


# --- объявления преподавателя ---

@teacher_router.get("/announcements", response_class=HTMLResponse)
async def teacher_form(request: Request, sent: int = 0, viewer: Viewer = Depends(teacher_viewer),
                       db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    return render(request, "announce.html", viewer, **await _form_ctx(db, viewer, "teacher", sent=bool(sent)))


@teacher_router.post("/announcements", response_class=HTMLResponse)
async def teacher_send(
    request: Request, title: str = Form(""), body: str = Form(""), group_id: int = Form(0),
    viewer: Viewer = Depends(teacher_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    values = {"title": title, "body": body, "group_id": group_id}
    try:
        group = await db.get(StudyGroup, group_id)  # право писать этой группе проверяет сервис
        await notes.create(db, user=viewer.user, source=Source.TEACHER, title=title, body=body,
                           audience=Audience.GROUP, group=group)
    except notes.AnnouncementError as e:
        resp = render(request, "announce.html", viewer, **await _form_ctx(db, viewer, "teacher", error=str(e), values=values))
        resp.status_code = 400
        return resp
    return RedirectResponse("/t/announcements?sent=1", status_code=303)
