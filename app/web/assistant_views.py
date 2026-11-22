"""Чат студента: сессия, CSRF и отдельные разрешения беседы до доступа к оценкам."""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Student

from app.assistant.service import BusyError, moscow_today
from app.assistant.personal import SqlPersonalEvidenceReader, SqlTeachingEvidenceReader
from app.assistant import teaching
from app.web.assistant_notifications import NOTIFICATION_CONTEXT, notification_answer, notification_filter
from app.web.deps import Viewer, check_csrf, current_viewer, get_db
from app.web.views import render
from app.web.i18n import language_for

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
    student_id: int | None = Field(default=None, gt=0)


def require_role(viewer):
    if viewer.user.role not in {"student", "teacher", "staff"}:
        raise HTTPException(404)


def demo_access(request):
    return request.app.state.assistant.cfg.assistant_demo_student_access


async def selected_student(request, viewer, db, student_id):
    query = select(Student.id).where(Student.id == student_id)
    if viewer.user.role == "teacher":
        query = query.where(Student.id.in_(teaching.student_ids(viewer.teacher_id)))
    elif not demo_access(request):
        raise HTTPException(403, "Доступ к демо-студентам отключён")
    if await db.scalar(query) is None:
        raise HTTPException(404)


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
async def conversation_action(request: Request, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)):
    require_role(viewer)
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
        student_id = None
        if viewer.user.role == "student":
            student_id = viewer.student_id
            if data.student_id is not None and data.student_id != student_id:
                raise HTTPException(404)
        elif data.student_id is not None:
            await selected_student(request, viewer, db, data.student_id)
            student_id = data.student_id
        try:
            token = store.start(owner, student_id, viewer.user.role, language_for(viewer))
            if viewer.user.role == "teacher":
                store.get(token, owner).teacher_id = viewer.teacher_id
        except RuntimeError:
            raise HTTPException(429, "Слишком много бесед. Повторите позднее") from None
        result = {"conversation_token": token}
        if viewer.user.role == "teacher" and student_id is not None:
            result["data_status"] = await teaching.coverage(db, viewer.teacher_id, student_id)
    else:
        conversation = store.get(data.conversation_token, owner)
        if conversation is None or conversation.actor_role != viewer.user.role:
            raise HTTPException(403, "Беседа завершена. Подтвердите предупреждение заново")
        if data.action == "allow_grades" and conversation.grades_pending:
            conversation.grant_grades()
        elif data.action == "clear":
            store.clear(data.conversation_token, owner)
        else:
            raise HTTPException(400, "Действие не запрошено чатом")
        result = {"ok": True}
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.get("/s/assistant")
async def assistant_page(request: Request, viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)):
    require_role(viewer)
    if viewer.user.role == "student":
        viewer.student_id
    response = render(request, "assistant.html", viewer, enabled=True, llm_enabled=request.app.state.assistant.enabled,
                      demo_access=demo_access(request) or viewer.user.role == "teacher")
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/s/assistant/students")
async def assistant_students(request: Request, q: str = "", viewer: Viewer = Depends(current_viewer), db: AsyncSession = Depends(get_db)):
    require_role(viewer)
    if viewer.user.role == "student" or (viewer.user.role != "teacher" and not demo_access(request)):
        raise HTTPException(404)
    if len(q) > 100:
        raise HTTPException(400)
    query = select(Student.id, Student.full_name).order_by(Student.full_name, Student.id).limit(30)
    if viewer.user.role == "teacher":
        query = query.where(Student.id.in_(teaching.student_ids(viewer.teacher_id)))
    if q.strip():
        query = query.where(Student.full_name.icontains(q.strip(), autoescape=True))
    rows = (await db.execute(query)).all()
    return JSONResponse({"students": [{"id": r.id, "name": r.full_name} for r in rows]}, headers={"Cache-Control": "no-store"})


@router.post("/s/assistant/ask")
async def assistant_ask(request: Request, viewer: Viewer = Depends(check_csrf), db: AsyncSession = Depends(get_db)):
    require_role(viewer)
    student_id = viewer.student_id if viewer.user.role == "student" else None
    body = await read_json(request)
    try:
        data = Question.model_validate_json(body)
        if not data.question.strip() or any(len(h) > 2000 for h in data.history):
            raise ValueError()
    except (ValidationError, ValueError):
        raise HTTPException(400, "Проверьте вопрос, историю и дату") from None
    conversation = request.app.state.assistant.conversations.get(data.conversation_token, viewer.auth_session.token_hash)
    if conversation is None or conversation.actor_role != viewer.user.role:
        raise HTTPException(403, "Сначала подтвердите предупреждение об ошибках ИИ")
    if viewer.user.role == "student":
        if conversation.student_id != student_id:
            raise HTTPException(404)
    else:
        student_id = conversation.student_id
        if viewer.user.role == "teacher" and conversation.teacher_id != viewer.teacher_id:
            raise HTTPException(404)
        if student_id is not None:
            await selected_student(request, viewer, db, student_id)
    from app.assistant.navigation import requested_service
    navigation = requested_service(data.question, viewer.user.role)
    if navigation:
        return JSONResponse({"status": "answer", "message": "Открываю: " + navigation["title"], "navigation": navigation}, headers={"Cache-Control": "no-store"})
    try:
        question = data.question.strip()
        previous = conversation.dialogue()
        filters = (notification_filter(question, previous[-1]["content"] if previous else "")
                   if viewer.user.role == "student" else None)
        if filters is not None:
            answer = await notification_answer(db, viewer, filters, data.as_of, moscow_today())
            # В дальнейший контекст модели не попадают заголовки или содержимое ленты.
            conversation.remember("Вопрос об уведомлениях", NOTIFICATION_CONTEXT)
        else:
            answer = await request.app.state.assistant.chat(viewer.user.id, student_id, question,
                data.history, data.as_of, conversation,
                SqlTeachingEvidenceReader(db, viewer.teacher_id) if viewer.user.role == "teacher" else SqlPersonalEvidenceReader(db))
    except BusyError:
        return JSONResponse({"status": "busy", "message": "Запрос уже выполняется или достигнут лимит. Подождите минуту."},
                            status_code=429, headers={"Retry-After": "60", "Cache-Control": "no-store"})
    return JSONResponse(answer, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/s/assistant/students/{student_id}/data-status")
async def teacher_data_status(student_id: int, request: Request, viewer: Viewer = Depends(current_viewer),
                              db: AsyncSession = Depends(get_db)):
    if viewer.user.role != "teacher":
        raise HTTPException(404)
    await selected_student(request, viewer, db, student_id)
    return JSONResponse(await teaching.coverage(db, viewer.teacher_id, student_id),
                        headers={"Cache-Control": "no-store"})
