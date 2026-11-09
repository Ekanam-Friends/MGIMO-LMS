"""Зависимости FastAPI для экранов кабинета: сессия БД, текущий пользователь, CSRF."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth
from app.models import Student, Teacher
from app.models.auth import AuthSession, User

COOKIE_NAME = "lms_session"


class LoginRequired(Exception):
    """Перехватывается в app.main и превращается в редирект на /login."""


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


@dataclass
class Viewer:
    user: User
    auth_session: AuthSession
    student: Student | None
    teacher: Teacher | None = None

    @property
    def csrf(self) -> str:
        return self.auth_session.csrf_token

    @property
    def student_id(self) -> int:
        """id студента для фильтра запросов. Только отсюда — никогда из параметров запроса."""
        if self.student is None:
            raise HTTPException(status_code=404)
        return self.student.id

    @property
    def teacher_id(self) -> int:
        """id преподавателя для фильтра ведомостей. Только отсюда — никогда из параметров запроса."""
        if self.teacher is None:
            raise HTTPException(status_code=404)
        return self.teacher.id


async def current_viewer(request: Request, db: AsyncSession = Depends(get_db)) -> Viewer:
    found = await auth.resolve(db, request.cookies.get(COOKIE_NAME))
    if found is None:
        raise LoginRequired
    user, auth_session = found
    student = await db.get(Student, user.student_id) if user.student_id else None
    teacher = await db.get(Teacher, user.teacher_id) if user.teacher_id else None
    return Viewer(user=user, auth_session=auth_session, student=student, teacher=teacher)


async def check_csrf(request: Request, viewer: Viewer = Depends(current_viewer)) -> Viewer:
    """Для POST-роутов кабинета: токен из скрытого поля формы или заголовка HTMX."""
    given = request.headers.get("X-CSRF-Token")
    if given is None:
        form = await request.form()
        given = form.get("csrf")  # type: ignore[assignment]
    if not auth.csrf_ok(viewer.csrf, given if isinstance(given, str) else None):
        raise HTTPException(status_code=403, detail="CSRF")
    return viewer
