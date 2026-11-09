"""Кабинет деканата как кабинет сотрудника (решение docs/decisions/0010-dean-employee-cabinet.md).

Общая часть «сотрудника» — главная, «Сервисы Академии», профиль, кадровые сервисы — та же, что у
преподавателя; своё у деканата — адресные объявления (app/web/notification_views.py). Все роуты —
только для роли staff, иначе 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications import service as notes
from app.web.deps import Viewer, get_db
from app.web.nav import SERVICES, STAFF_WORK, TEACHER_RESOURCES
from app.web.notification_views import staff_viewer
from app.web.views import render

router = APIRouter(prefix="/d")


@router.get("/home", response_class=HTMLResponse)
async def staff_home(request: Request, viewer: Viewer = Depends(staff_viewer),
                     db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    mine = await notes.own(db, viewer.user.id, limit=5)
    return render(request, "staff/home.html", viewer, mine=mine, work=STAFF_WORK, resources=TEACHER_RESOURCES)


@router.get("/services", response_class=HTMLResponse)
async def staff_services(request: Request, viewer: Viewer = Depends(staff_viewer)) -> HTMLResponse:
    return render(request, "teacher/services.html", viewer, resources=TEACHER_RESOURCES, services=SERVICES,
                  home_href="/d/home")


@router.get("/students/search", response_class=HTMLResponse)
async def student_search(request: Request, student_q: str = "", viewer: Viewer = Depends(staff_viewer),
                         db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Подсказки адресата для формы объявления (HTMX): только ФИО и группа."""
    found = await notes.search_students(db, student_q)
    return render(request, "partials/_student_results.html", viewer, found=found, q=student_q.strip())
