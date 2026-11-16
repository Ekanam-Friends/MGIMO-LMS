"""Чат студента: сессия, CSRF и отдельные разрешения беседы до доступа к оценкам."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.service import BusyError
from app.web.deps import Viewer, check_csrf, current_viewer, get_db
from app.web.views import render

router = APIRouter()


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=2000)
    history: list[str] = Field(default_factory=list, max_length=4)
    as_of: date | None = None
    conversation_token: str = Field(default="", max_length=100)


class ConversationAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str
    acknowledged: bool = False
    conversation_token: str = Field(default="", max_length=100)


async def read_json(request: Request):
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        raise HTTPException(415, "Нужен JSON")
    body = bytearray()
    async for block in request.stream():
        body.extend(block)
        if len(body) > 24000:
            raise HTTPException(413, "Слишком большой запрос")
    return body


@router.post("/s/assistant/conversation")
async def conversation_action(request: Request, viewer: Viewer = Depends(check_csrf)):
    if viewer.user.role != "student":
        raise HTTPException(404)
    viewer.student_id
    body = await read_json(request)
    try:
        data = ConversationAction.model_validate_json(body)
    except ValidationError:
        raise HTTPException(400, "Проверьте действие беседы") from None
    store = request.app.state.assistant.conversations
    owner = viewer.auth_session.token_hash
    if data.action == "start":
        if not data.acknowledged:
            raise HTTPException(400, "Сначала подтвердите, что ИИ может ошибаться")
        try:
            token = store.start(owner)
        except RuntimeError:
            raise HTTPException(429, "Слишком много бесед. Повторите позднее") from None
        result = {"conversation_token": token}
    else:
        conversation = store.get(data.conversation_token, owner)
        if conversation is None:
            raise HTTPException(403, "Беседа завершена. Подтвердите предупреждение заново")
        if data.action == "allow_grades" and conversation.grades_pending:
            conversation.grades_allowed = True
            conversation.grades_pending = False
        elif data.action == "clear":
            store.clear(data.conversation_token, owner)
        else:
            raise HTTPException(400, "Действие не запрошено чатом")
        result = {"ok": True}
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/s/assistant")
async def assistant_page(request: Request, viewer: Viewer = Depends(current_viewer)):
    if viewer.user.role != "student":
        raise HTTPException(404)
    viewer.student_id  # отделение публичного RAG от кабинетов преподавателя и сотрудника
    response = render(request, "assistant.html", viewer, enabled=request.app.state.assistant.enabled)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/s/assistant/ask")
async def assistant_ask(request: Request, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)):
    if viewer.user.role != "student":
        raise HTTPException(404)
    student_id = viewer.student_id
    body = await read_json(request)
    try:
        data = Question.model_validate_json(body)
        if not data.question.strip() or any(len(h) > 2000 for h in data.history):
            raise ValueError()
    except (ValidationError, ValueError):
        raise HTTPException(400, "Проверьте вопрос, историю и дату") from None
    conversation = request.app.state.assistant.conversations.get(data.conversation_token, viewer.auth_session.token_hash)
    if conversation is None:
        raise HTTPException(403, "Сначала подтвердите предупреждение об ошибках ИИ")
    try:
        answer = await request.app.state.assistant.chat(viewer.user.id, student_id, data.question.strip(),
            data.history, data.as_of, conversation, db)
    except BusyError:
        return JSONResponse({"status": "busy", "message": "Запрос уже выполняется или достигнут лимит. Подождите минуту."},
                            status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})
    return JSONResponse(answer, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
