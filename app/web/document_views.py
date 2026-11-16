"""База знаний деканата: загрузка, очередь и повтор обработки."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import defer
from starlette.datastructures import UploadFile

from app.auth import service as auth
from app.assistant.documents import DocumentError, DocumentMetadata, MAX_FILE_BYTES, upload_document
from app.models.knowledge import DocumentStatus as Status, KnowledgeDocument
from app.web.deps import Viewer, get_db
from app.web.notification_views import staff_csrf, staff_viewer
from app.web.views import render

router = APIRouter(prefix="/d/documents")


class UploadTooLarge(Exception):
    pass


class DocumentBodyLimit:
    """Лимит действует и без Content-Length, до записи большого тела на диск."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/d/documents":
            return await self.app(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_FILE_BYTES + 64_000:
                raise UploadTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except UploadTooLarge:
            response = HTMLResponse(
                '<!doctype html><html lang="ru"><head><meta name="viewport" content="width=device-width, initial-scale=1">'
                '<title>Файл слишком большой · концепт</title><link rel="stylesheet" href="/static/app.css"></head>'
                '<body><div class="concept-bar">Концепт для конкурса — не официальный сервис.</div>'
                '<main class="panel"><h1>Файл слишком большой</h1>'
                '<p>Размер загрузки превышает 10 МБ. Выберите меньший PDF.</p>'
                '<a href="/d/documents">Вернуться к загрузке</a></main></body></html>', status_code=413)
            await response(scope, receive, send)


async def document_page(request, viewer, db, *, error=None, values=None, status_code=200):
    rows = (await db.scalars(select(KnowledgeDocument).options(defer(KnowledgeDocument.data))
        .order_by(KnowledgeDocument.id.desc()).limit(100))).all()
    response = render(request, "staff/documents.html", viewer, documents=rows,
                      statuses=Status.TITLES, error=error, values=values or {})
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    return response


async def upload_csrf(request: Request, viewer: Viewer = Depends(staff_viewer)):
    """Права проверяются до чтения файла; временные файлы закрываются и при отказе CSRF."""
    try:
        form = await request.form(max_files=1, max_fields=8)
        given = request.headers.get("X-CSRF-Token") or form.get("csrf")
        if not auth.csrf_ok(viewer.csrf, given if isinstance(given, str) else None):
            raise HTTPException(status_code=403, detail="CSRF")
        yield viewer
    finally:
        await request.close()


@router.get("", response_class=HTMLResponse)
async def documents(request: Request, viewer: Viewer = Depends(staff_viewer), db=Depends(get_db)):
    return await document_page(request, viewer, db)


@router.post("")
async def upload(request: Request, viewer: Viewer = Depends(upload_csrf), db=Depends(get_db)):
    form = await request.form()
    file = form.get("file")
    values = {key: str(form.get(key, "")) for key in ("title", "source_url", "revision")}
    try:
        if not isinstance(file, UploadFile):
            raise DocumentError("Выберите PDF-файл")
        metadata = DocumentMetadata(**values, reviewed_public=form.get("reviewed_public") == "yes")
        data = await file.read(MAX_FILE_BYTES + 1)
        row, created = await upload_document(db, viewer.user, file.filename or "", data, metadata)
        await db.commit()
        return RedirectResponse(f"/d/documents?{'uploaded' if created else 'duplicate'}={row.id}#document-{row.id}", status_code=303)
    except ValidationError as exc:
        messages = {"title": "Укажите название (до 500 символов)",
                    "revision": "Укажите обозначение редакции (до 120 символов)",
                    "source_url": "Укажите публичную HTTPS-ссылку на сайт mgimo.ru (до 2000 символов)"}
        error = messages.get(str(exc.errors()[0]["loc"][0]), "Проверьте параметры документа")
        return await document_page(request, viewer, db, error=error, values=values, status_code=400)
    except DocumentError as exc:
        return await document_page(request, viewer, db, error=str(exc), values=values, status_code=400)
    finally:
        await form.close()


@router.post("/{document_id}/retry")
async def retry(document_id: int, viewer: Viewer = Depends(staff_csrf), db=Depends(get_db)):
    row = await db.get(KnowledgeDocument, document_id)
    if row is None:
        raise HTTPException(status_code=404)
    if row.status == Status.ERROR:
        row.status, row.error = Status.QUEUED, None
        await db.commit()
    return RedirectResponse(f"/d/documents#document-{row.id}", status_code=303)
