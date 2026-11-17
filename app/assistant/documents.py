"""Загрузка публичных PDF и последовательная публикация текстового индекса.

Один обработчик на процесс web, как и сверка Pyrus. Очередь хранится в БД;
после перезапуска незавершённые операции повторяются идемпотентно.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.auth.service import utcnow
from app.models.auth import Role, User
from app.models.knowledge import DocumentStatus as Status, KnowledgeDocument
from .corpus import Chunk, Corpus, CorpusError, public_url
from app.assistant.repositories import load_corpus
from .excerpts import document_excerpts, ExcerptTooLong

log = logging.getLogger(__name__)
MAX_FILE_BYTES = 10_000_000
MAX_PAGES = 100


class DocumentError(ValueError):
    """Ошибка, которую можно показать работнику."""


class ReviewRequired(DocumentError):
    pass


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(min_length=1, max_length=2000)
    revision: str = Field(min_length=1, max_length=120)
    reviewed_public: bool

    @field_validator("source_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return public_url(value)


def require_staff(actor: User) -> None:
    if actor.role != Role.STAFF:
        raise DocumentError("Загрузка доступна только сотруднику деканата")


async def upload_document(db, actor: User, filename: str, data: bytes,
                          metadata: DocumentMetadata) -> tuple[KnowledgeDocument, bool]:
    require_staff(actor)
    if not metadata.reviewed_public:
        raise DocumentError("Подтвердите, что файл сверен с публичным источником и не содержит персональных данных")
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not filename.lower().endswith(".pdf") or not data.startswith(b"%PDF-"):
        raise DocumentError("Загрузите PDF-файл")
    if not 1 <= len(data) <= MAX_FILE_BYTES:
        raise DocumentError("Размер PDF должен быть не больше 10 МБ")
    digest = hashlib.sha256(data).hexdigest()
    source_key = hashlib.sha256(metadata.source_url.encode()).hexdigest()

    async def duplicate():
        same_file = await db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.sha256 == digest))
        return same_file or await db.scalar(select(KnowledgeDocument).where(
            KnowledgeDocument.source_key == source_key).with_for_update())

    existing = await duplicate()
    if existing:
        if existing.sha256 != digest:
            if existing.status not in {Status.ERROR, Status.NEEDS_REVIEW}:
                raise DocumentError("Документ с этой ссылкой уже загружен. Замена опубликованной редакции в этой версии не поддерживается")
            # Исправленный файл может заменить неуспешную загрузку, не рабочий источник.
            try:
                async with db.begin_nested():
                    existing.data, existing.sha256, existing.filename = data, digest, filename[:255]
                    existing.title, existing.revision = metadata.title, metadata.revision
                    existing.uploaded_by_user_id, existing.uploaded_at = actor.id, utcnow()
                    existing.status, existing.error = Status.QUEUED, None
                    existing.chunk_count, existing.indexed_at = 0, None
                    await db.flush()
            except IntegrityError:
                existing = await duplicate()
                if existing and existing.sha256 == digest:
                    return existing, False
                raise DocumentError("Документ уже изменён другим сотрудником. Обновите список") from None
            return existing, True
        return existing, False
    row = KnowledgeDocument(
        uploaded_by_user_id=actor.id, filename=filename[:255], data=data, sha256=digest,
        source_key=source_key, title=metadata.title, source_url=metadata.source_url,
        revision=metadata.revision, reviewed_public=True, status=Status.QUEUED,
        chunk_count=0, uploaded_at=utcnow(),
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await duplicate()
        if existing and existing.sha256 == digest:
            return existing, False
        raise DocumentError("Документ с этой ссылкой уже загружен") from None
    return row, True


def extract_chunks(row: KnowledgeDocument) -> list[Chunk]:
    try:
        import pymupdf
    except ImportError:
        raise DocumentError("На сервере не установлен обработчик PDF. Сообщите администратору") from None
    checked_on = row.uploaded_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Moscow")).date()
    pages = []
    try:
        with pymupdf.open(stream=row.data, filetype="pdf") as doc:
            if doc.is_encrypted or not 1 <= len(doc) <= MAX_PAGES:
                raise DocumentError("PDF должен быть без пароля и содержать от 1 до 100 страниц")
            for page_no, page in enumerate(doc, 1):
                text = page.get_text(sort=True).strip()
                if len(re.findall(r"[а-яёa-z]", text, re.I)) < 40:
                    raise ReviewRequired(f"На странице {page_no} недостаточно текста. Выполните OCR, сверьте текст и загрузите PDF с текстовым слоем")
                pages.append(text)
            try:
                excerpts = document_excerpts(pages)
            except ExcerptTooLong as exc:
                raise ReviewRequired(str(exc)) from None
            chunks = []
            for number, excerpt in enumerate(excerpts):
                chunks.append(Chunk(
                        id=f"upload:{row.sha256[:32]}:{number}",
                        document_id=f"upload:{row.sha256}", revision=row.revision, title=row.title,
                        source_url=row.source_url, source_sha256=row.sha256,
                        document_date=None, effective_from=None, checked_at=checked_on,
                        downloaded_at=checked_on, reviewed_public=row.reviewed_public,
                        verification_status="snapshot", page=excerpt.page, page_end=excerpt.page_end,
                        clause=f"стр. {excerpt.page}–{excerpt.page_end}", text=excerpt.text,
                    ))
    except (DocumentError, CorpusError):
        raise
    except Exception:
        raise DocumentError("Не удалось прочитать PDF. Проверьте файл и повторите загрузку") from None
    return chunks


def publish_chunks(directory: Path, added: list[Chunk]) -> int:
    """Проверить объединённый снимок и заменить его одним атомарным rename."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "chunks.jsonl"
    existing = load_corpus(target).chunks if target.exists() else []
    matching = [c for c in existing if c.source_url == added[0].source_url
                or c.source_sha256 == added[0].source_sha256]
    if matching:
        if any(c.source_sha256 != added[0].source_sha256 for c in matching):
            raise DocumentError("В рабочей базе уже есть другая версия этого источника. Обратитесь к администратору для замены редакции")
        # Повтор после сбоя между rename и commit не дублирует фрагменты.
        return len(matching)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix="chunks-upload-", suffix=".jsonl",
                                         mode="w", encoding="utf-8", delete=False) as stream:
            pending = Path(stream.name)
            for chunk in [*existing, *added]:
                stream.write(chunk.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        load_corpus(pending)
        os.replace(pending, target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return len(added)


class DocumentProcessor:
    def __init__(self, session_factory, cfg):
        self.session_factory, self.cfg = session_factory, cfg
        self.stop = asyncio.Event()

    async def process_one(self) -> bool:
        async with self.session_factory() as db:
            row = await db.scalar(select(KnowledgeDocument).where(
                KnowledgeDocument.status == Status.QUEUED).order_by(KnowledgeDocument.id)
                .limit(1).with_for_update(skip_locked=True))
            if row is None:
                return False
            row.status, row.error = Status.PROCESSING, None
            await db.commit()
            try:
                if self.cfg.rag_use_embeddings:
                    raise DocumentError("Загрузка поддерживает текстовый поиск. Для векторного режима требуется подготовка администратором")
                chunks = await asyncio.to_thread(extract_chunks, row)
                count = await asyncio.to_thread(publish_chunks, Path(self.cfg.rag_corpus_dir), chunks)
                row.status, row.chunk_count, row.indexed_at = Status.PUBLISHED, count, utcnow()
            except ReviewRequired as exc:
                row.status, row.error = Status.NEEDS_REVIEW, str(exc)
            except DocumentError as exc:
                row.status, row.error = Status.ERROR, str(exc)
            except Exception:
                log.exception("Не удалось обработать документ базы знаний id=%s", row.id)
                row.status, row.error = Status.ERROR, "Не удалось обновить базу знаний. Повторите обработку или сообщите администратору"
            await db.commit()
        return True

    async def run(self) -> None:
        # В web используется ровно один процесс; processing принадлежит прошлому запуску.
        async with self.session_factory() as db:
            await db.execute(update(KnowledgeDocument).where(
                KnowledgeDocument.status == Status.PROCESSING).values(status=Status.QUEUED))
            await db.commit()
        while not self.stop.is_set():
            try:
                if await self.process_one():
                    continue
            except Exception:
                log.exception("Очередь документов временно недоступна")
            try:
                await asyncio.wait_for(self.stop.wait(), 2)
            except asyncio.TimeoutError:
                pass
