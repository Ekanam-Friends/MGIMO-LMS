"""Чат студента: аутентификация, CSRF, лимиты; история и профиль не хранятся в LLM."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.assistant.service import BusyError
from app.web.deps import Viewer, check_csrf, current_viewer
from app.web.views import render

router = APIRouter()


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=2000)
    history: list[str] = Field(default_factory=list, max_length=4)
    as_of: date | None = None


@router.get("/s/assistant")
async def assistant_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    if viewer.user.role != "student":
        raise HTTPException(404)
    viewer.student_id  # отделение публичного RAG от кабинетов преподавателя и сотрудника
    response = render(request, "assistant.html", viewer, enabled=request.app.state.assistant.enabled)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/s/assistant/ask")
async def assistant_ask(request: Request, viewer: Viewer = Depends(check_csrf)):
    if viewer.user.role != "student":
        raise HTTPException(404)
    viewer.student_id
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Нужен JSON")
    body = bytearray()
    async for block in request.stream():
        body.extend(block)
        if len(body) > 24000:
            raise HTTPException(413, "Слишком большой запрос")
    try:
        data = Question.model_validate_json(body)
        if not data.question.strip() or any(len(h) > 2000 for h in data.history):
            raise ValueError()
    except (ValidationError, ValueError):
        raise HTTPException(400, "Проверьте вопрос, историю и дату") from None
    try:
        answer = await request.app.state.assistant.ask(viewer.user.id, data.question.strip(), data.history, data.as_of)
    except BusyError:
        return JSONResponse({"status": "busy", "message": "Запрос уже выполняется или достигнут лимит. Подождите минуту."},
                            status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})
    return JSONResponse(answer, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
