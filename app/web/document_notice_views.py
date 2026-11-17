"""Предложение ИИ, ручная правка, предпросмотр и явное подтверждение рассылки."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import defer

from app.models import Student
from app.models.document_notice import DocumentNotice
from app.models.knowledge import KnowledgeDocument
from app.notifications import document_service as service
from app.notifications.document_repository import SqlNoticeRepository
from app.assistant.repositories import JsonlCorpusRepository
from app.web.deps import Viewer, get_db
from app.web.notification_views import staff_csrf, staff_viewer
from app.web.views import render

router = APIRouter(prefix="/d/documents")


async def get_document(db, document_id):
    doc = await db.scalar(select(KnowledgeDocument).options(defer(KnowledgeDocument.data))
                          .where(KnowledgeDocument.id == document_id))
    if doc is None:
        raise HTTPException(status_code=404)
    return doc


async def page(request, viewer, db, doc, *, selected=None, error=None, code=200):
    notice = await db.get(DocumentNotice, doc.id)
    options = await SqlNoticeRepository(db).options()
    selected = selected if selected is not None else (notice.suggested_targets if notice else [])
    preview_labels = []
    if notice and notice.preview_targets and notice.preview_by == viewer.user.id:
        by_key = {o.key: o.label for o in options}
        for key in notice.preview_targets:
            if key.startswith("student:"):
                student = await db.get(Student, int(key[8:]))
                preview_labels.append("Студент: " + (student.full_name if student else "не найден"))
            else:
                preview_labels.append(by_key.get(key, "Аудитория изменилась"))
    response = render(request, "staff/document_notice.html", viewer, doc=doc, notice=notice,
        options=options, selected=selected, error=error, preview_labels=preview_labels)
    response.status_code = code
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/{document_id}/notify")
async def notice_page(document_id: int, request: Request, viewer: Viewer = Depends(staff_viewer), db=Depends(get_db)):
    return await page(request, viewer, db, await get_document(db, document_id))


@router.post("/{document_id}/notify/{action}")
async def notice_action(document_id: int, action: str, request: Request,
                        viewer: Viewer = Depends(staff_csrf), db=Depends(get_db)):
    doc = await get_document(db, document_id)
    repo = SqlNoticeRepository(db)
    form = await request.form()
    selected = list(dict.fromkeys(str(v) for v in form.getlist("targets")))
    student_id = form.get("student_id")
    if isinstance(student_id, str) and student_id:
        selected.append("student:" + student_id)
    try:
        if action == "suggest":
            await service.suggest(repo, doc, viewer.user, JsonlCorpusRepository(
                Path(request.app.state.settings.rag_corpus_dir) / "chunks.jsonl"),
                                  request.app.state.assistant.provider)
            return RedirectResponse(f"/d/documents/{document_id}/notify", status_code=303)
        if action == "preview":
            await service.preview(repo, doc, viewer.user, selected)
            return await page(request, viewer, db, doc, selected=selected)
        if action == "confirm":
            await service.confirm(repo, doc, viewer.user, str(form.get("preview_token", "")))
            return RedirectResponse(f"/d/documents/{document_id}/notify", status_code=303)
        raise HTTPException(status_code=404)
    except service.NoticeError as exc:
        return await page(request, viewer, db, doc, selected=selected, error=str(exc), code=400)
