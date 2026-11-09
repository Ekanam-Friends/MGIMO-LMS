"""Экраны личного кабинета (серверные шаблоны Jinja2 + HTMX).

Вёрстка повторяет ЛК студента РАНХиГС (docs/research/ranepa-lk.md) как временный эталон —
см. docs/decisions/0004-web-shell.md. Разделы без бэкенда — страницы-каркасы с описанием,
что туда подключится.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth
from app.bot import ics
from app.bot import link as tg_link
from app.certificates import service as certs
from app.models import StudyGroup
from app.models.certificates import CertificateKind, RequestStatus
from app.web import calendar_view as cv
from app.web import schedule_view as sv
from app.web.deps import COOKIE_NAME, Viewer, check_csrf, current_viewer, get_db
from app.web.nav import MENU, SERVICES, STUBS, TABBAR

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(MENU=MENU, TABBAR=TABBAR, sv=sv, cv=cv, KINDS=CertificateKind.TITLES)
# Версия стилей в адресе: после выкладки браузер не держит старый app.css из кэша.
_CSS = Path(__file__).parent / "static" / "app.css"
templates.env.globals["css_version"] = lambda: int(_CSS.stat().st_mtime)

router = APIRouter()


def _fmt_date(d: date | datetime | None) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def _fmt_time(t) -> str:
    return t.strftime("%H:%M") if t else ""


templates.env.filters["ddmmyyyy"] = _fmt_date
templates.env.filters["hhmm"] = _fmt_time


def today() -> date:
    return date.today()


def render(request: Request, name: str, viewer: Viewer | None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"viewer": viewer, "path": request.url.path, **ctx})


def _parse_day(raw: str | None, default: date) -> date:
    try:
        return date.fromisoformat(raw) if raw else default
    except ValueError:
        return default


# --- вход и выход ---


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return render(request, "login.html", None, error=None, login="")


@router.post("/login")
async def login(
    request: Request, login: str = Form(""), password: str = Form(""), db: AsyncSession = Depends(get_db)
) -> Response:
    user = await auth.authenticate(db, login, password)
    if user is None:
        return templates.TemplateResponse(
            request, "login.html", {"viewer": None, "path": "/login", "error": "Неверный логин или пароль", "login": login},
            status_code=400,
        )
    cfg = request.app.state.settings
    token = await auth.open_session(db, user, ttl=timedelta(days=cfg.session_days))
    await db.commit()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, token, max_age=cfg.session_days * 86400, httponly=True,
        secure=cfg.cookie_secure, samesite="lax", path="/",
    )
    return resp


@router.post("/logout")
async def logout(request: Request, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)) -> Response:
    await auth.close_session(db, request.cookies.get(COOKIE_NAME))
    await db.commit()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# --- главная и расписание: календарь из трёх слоёв (решение 0006) ---


async def _day_widget_ctx(db: AsyncSession, viewer: Viewer, day: date) -> dict:
    """Список дня — мобильный вид календаря на главной."""
    start = sv.week_start(day)
    week = [start + timedelta(days=i) for i in range(7)]
    events = await cv.events_for(db, viewer.student, week[0], week[-1]) if viewer.student else {}
    return {"day": day, "week": week, "day_events": events.get(day, []), "busy": {d for d, e in events.items() if e},
            "today": today(), "prev_week": start - timedelta(days=7), "next_week": start + timedelta(days=7)}


async def _calendar_ctx(db: AsyncSession, viewer: Viewer, anchor: date, size: str) -> dict:
    """Недельная сетка (десктоп)."""
    start = sv.week_start(anchor)
    days = [start + timedelta(days=i) for i in range(7)]
    student = viewer.student
    events = await cv.events_for(db, student, days[0], days[-1]) if student else {}
    my_subgroups, my_disciplines = await cv.get_choices(db, student.id) if student else (set(), set())
    return {"days": days, "events": events, "today": today(), "size": "full" if size == "full" else "compact",
            "prev_week": start - timedelta(days=7), "next_week": start + timedelta(days=7),
            "hours": list(range(cv.GRID_FROM, cv.GRID_TO)), "has_choices": bool(my_subgroups or my_disciplines),
            "has_group": bool(student and student.study_group_id)}


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, d: str | None = None, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    if viewer.student is None:
        return render(request, "staff.html", viewer)
    day = _parse_day(d, today())
    requests = (await certs.list_requests(db, viewer.student_id))[:2]
    return render(
        request, "dashboard.html", viewer, requests=requests, certs=certs,
        cal=await _calendar_ctx(db, viewer, day, "compact"), **await _day_widget_ctx(db, viewer, day),
    )


@router.get("/partials/day", response_class=HTMLResponse)
async def day_partial(
    request: Request, d: str | None = None, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    day = _parse_day(d, today())
    return render(request, "partials/day_schedule.html", viewer, **await _day_widget_ctx(db, viewer, day))


@router.get("/partials/calendar", response_class=HTMLResponse)
async def calendar_partial(
    request: Request, week: str | None = None, size: str = "compact",
    viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    return render(request, "partials/calendar_week.html", viewer,
                  cal=await _calendar_ctx(db, viewer, _parse_day(week, today()), size))


@router.get("/schedule", response_class=HTMLResponse)
async def schedule(
    request: Request, week: str | None = None, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    cal = await _calendar_ctx(db, viewer, _parse_day(week, today()), "full")
    return render(request, "schedule.html", viewer, cal=cal, days=cal["days"], events=cal["events"], today=cal["today"],
                  prev_week=cal["prev_week"], next_week=cal["next_week"], has_group=cal["has_group"])


@router.post("/activities/{activity_id}/{action}")
async def activity_signup(
    request: Request, activity_id: int, action: str, week: str = Form(""), size: str = Form("compact"),
    back: str = Form("/"), viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    """Запись на активность и отмена — только за студента из сессии."""
    if action not in ("signup", "cancel") or viewer.student is None:
        raise HTTPException(status_code=404)
    activity = await cv.set_signup(db, viewer.student_id, activity_id, on=action == "signup")
    if activity is None:
        raise HTTPException(status_code=404)
    await db.commit()
    anchor = _parse_day(week, activity.starts_at.date())
    if request.headers.get("HX-Request"):
        return render(request, "partials/calendar_week.html", viewer, cal=await _calendar_ctx(db, viewer, anchor, size))
    # Без JS — назад на свою страницу (только из белого списка, не открытый редирект).
    target = "/schedule" if back == "/schedule" else "/"
    return RedirectResponse(f"{target}?{'week' if target == '/schedule' else 'd'}={anchor.isoformat()}", status_code=303)


# --- элективы и подгруппы ---


@router.get("/electives", response_class=HTMLResponse)
async def electives(
    request: Request, saved: int | None = None, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    student = viewer.student
    catalog = await cv.elective_catalog(db, student.study_group_id if student else None)
    my_subgroups, my_disciplines = await cv.get_choices(db, student.id) if student else (set(), set())
    return render(request, "electives.html", viewer, catalog=catalog, my_subgroups=my_subgroups,
                  my_disciplines=my_disciplines, saved=saved, has_group=bool(student and student.study_group_id))


@router.post("/electives")
async def electives_save(request: Request, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)) -> Response:
    if viewer.student is None:
        raise HTTPException(status_code=404)
    form = await request.form()
    catalog = await cv.elective_catalog(db, viewer.student.study_group_id)
    await cv.set_choices(db, viewer.student_id, catalog,
                         [str(v) for v in form.getlist("subgroup")], [str(v) for v in form.getlist("discipline")])
    await db.commit()
    return RedirectResponse("/electives?saved=1", status_code=303)


# --- профиль ---


@router.get("/profile", response_class=HTMLResponse)
async def profile(request: Request, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    group = None
    if viewer.student and viewer.student.study_group_id:
        group = await db.get(StudyGroup, viewer.student.study_group_id)
    return render(request, "profile.html", viewer, student=viewer.student, group=group,
                  **await _telegram_ctx(request, viewer, db))


async def _telegram_ctx(request: Request, viewer: Viewer, db: AsyncSession) -> dict:
    """Данные блока «Telegram-бот» (partials/_telegram.html): привязка только своего студента."""
    tg = await tg_link.by_student(db, viewer.student.id) if viewer.student else None
    cfg = request.app.state.settings
    return {"tg": tg, "bot_username": cfg.telegram_bot_username if cfg.bot_link_enabled else "",
            "code_minutes": cfg.telegram_link_code_minutes}


@router.get("/services", response_class=HTMLResponse)
async def services(request: Request, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    ctx = await _telegram_ctx(request, viewer, db)
    cfg = request.app.state.settings
    tg = ctx["tg"]
    return render(request, "services.html", viewer, student=viewer.student, services=SERVICES,
                  feed_url=cfg.feed_url(tg.feed_token) if tg else "",
                  webcal_url=cfg.webcal_url(tg.feed_token) if tg else "", **ctx)


# --- Telegram-бот (решение 0005) ---


@router.post("/profile/telegram/link")
async def telegram_link(request: Request, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)) -> Response:
    cfg = request.app.state.settings
    if not cfg.bot_link_enabled:
        raise HTTPException(status_code=404)
    code = await tg_link.issue_code(db, viewer.student_id, ttl=timedelta(minutes=cfg.telegram_link_code_minutes))
    await db.commit()
    return RedirectResponse(f"https://t.me/{cfg.telegram_bot_username}?start={code}", status_code=303)


@router.post("/profile/telegram/unlink")
async def telegram_unlink(back: str = Form("/profile"), viewer: Viewer = Depends(check_csrf),
                         db: AsyncSession = Depends(get_db)) -> Response:
    await tg_link.unlink(db, viewer.student_id)
    await db.commit()
    # Возврат только на свои страницы: произвольный адрес из формы — открытый редирект.
    return RedirectResponse(back if back in ("/profile", "/services") else "/profile", status_code=303)


@router.get("/feed/{token}.ics")
async def calendar_feed(token: str, db: AsyncSession = Depends(get_db)) -> Response:
    """Лента подписки. Без входа: календари не умеют cookie, доступ — по секрету в адресе."""
    found = await tg_link.by_feed_token(db, token)
    if found is None:
        raise HTTPException(status_code=404)
    link, student = found
    start = today()
    body = await ics.build_calendar(db, student.study_group_id, start=start - ics.FEED_BACK, end=start + ics.FEED_AHEAD,
                                    for_subscription=True, reminder_minutes=link.reminder_minutes)
    return Response(body, media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "private, max-age=900", "Content-Disposition": 'inline; filename="mgimo.ics"',
                             "X-Robots-Tag": "noindex"})


# --- справки (студенческий офис) ---


@router.get("/mfc/certificates", response_class=HTMLResponse)
async def certificates(
    request: Request, created: int | None = None, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    rows = await certs.list_requests(db, viewer.student_id)
    return render(
        request, "certificates.html", viewer, rows=rows, certs=certs, kinds=CertificateKind.TITLES,
        paper_only=CertificateKind.PAPER_ONLY, created=created, error=None, RequestStatus=RequestStatus,
    )


@router.post("/mfc/certificates")
async def certificate_create(
    request: Request,
    kind: str = Form(...),
    purpose: str = Form(""),
    copies: int = Form(1),
    viewer: Viewer = Depends(check_csrf),
    db: AsyncSession = Depends(get_db),
) -> Response:
    try:
        req = await certs.create_request(db, viewer.student_id, kind=kind, purpose=purpose, copies=copies)
    except certs.ValidationError as e:
        rows = await certs.list_requests(db, viewer.student_id)
        return templates.TemplateResponse(
            request, "certificates.html",
            {"viewer": viewer, "path": request.url.path, "rows": rows, "certs": certs,
             "kinds": CertificateKind.TITLES, "paper_only": CertificateKind.PAPER_ONLY, "created": None,
             "error": str(e), "RequestStatus": RequestStatus},
            status_code=400,
        )
    await db.commit()
    return RedirectResponse(f"/mfc/certificates?created={req.id}", status_code=303)


@router.get("/mfc/certificates/{request_id}", response_class=HTMLResponse)
async def certificate_detail(
    request: Request, request_id: int, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    req = await certs.get_request(db, viewer.student_id, request_id)
    if req is None:
        raise HTTPException(status_code=404)
    events = await certs.get_events(db, viewer.student_id, request_id) or []
    return render(request, "certificate_detail.html", viewer, req=req, events=events, certs=certs,
                  kinds=CertificateKind.TITLES, RequestStatus=RequestStatus, error=None)


@router.get("/mfc/certificates/{request_id}/pdf")
async def certificate_pdf(
    request_id: int, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)
) -> Response:
    pdf = await certs.download_pdf(db, viewer.student_id, request_id)
    if pdf is None:
        raise HTTPException(status_code=404)
    await db.commit()
    name = quote(f"spravka-{request_id}.pdf")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


@router.post("/mfc/certificates/{request_id}/paper")
async def certificate_paper(
    request_id: int, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)
) -> Response:
    try:
        req = await certs.request_paper(db, viewer.student_id, request_id)
    except certs.ValidationError:
        raise HTTPException(status_code=409)
    if req is None:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse("/mfc/certificates", status_code=303)


@router.post("/mfc/certificates/{request_id}/reply")
async def certificate_reply(
    request: Request, request_id: int, text: str = Form(""),
    viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db),
) -> Response:
    form = await request.form()
    files = [
        certs.UploadedFile(f.filename or "file", await f.read(), f.content_type or "application/octet-stream")
        for f in form.getlist("files") if hasattr(f, "read") and getattr(f, "filename", "")
    ]
    try:
        reply = await certs.add_reply(db, viewer.student_id, request_id, text=text, files=files)
    except certs.ValidationError as e:
        req = await certs.get_request(db, viewer.student_id, request_id)
        if req is None:
            raise HTTPException(status_code=404)
        events = await certs.get_events(db, viewer.student_id, request_id) or []
        return templates.TemplateResponse(
            request, "certificate_detail.html",
            {"viewer": viewer, "path": request.url.path, "req": req, "events": events, "certs": certs,
             "kinds": CertificateKind.TITLES, "RequestStatus": RequestStatus, "error": str(e)},
            status_code=400,
        )
    if reply is None:
        raise HTTPException(status_code=404)
    await db.commit()
    return RedirectResponse(f"/mfc/certificates/{request_id}", status_code=303)


# --- разделы-каркасы ---


@router.get("/s/{slug}", response_class=HTMLResponse)
async def stub(request: Request, slug: str, viewer: Viewer = Depends(current_viewer)) -> HTMLResponse:
    page = STUBS.get(slug)
    if page is None:
        raise HTTPException(status_code=404)
    return render(request, "stub.html", viewer, page=page)
