"""Загрузка, права, восстановление очереди и сохранность рабочего корпуса."""
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.assistant import documents as service
from app.assistant.corpus import Corpus
from app.models.auth import Role, User
from app.models.knowledge import DocumentStatus as Status, KnowledgeDocument
from tests.test_web import client, login

TEXT = "Students submit an application to the dean office for an academic leave. The decision is issued after review. " * 3
REAL_RUN = service.DocumentProcessor.run


def pdf_bytes(text=TEXT):
    import pymupdf
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox((50, 50, 500, 700), text)
        return doc.tobytes()


@pytest.fixture(autouse=True)
def pause_background(monkeypatch):
    async def paused(self):
        await self.stop.wait()
    monkeypatch.setattr(service.DocumentProcessor, "run", paused)


async def as_staff(client):
    csrf = await login(client)
    async with client._transport.app.state.session_factory() as db:
        await db.execute(update(User).where(User.login == "anna").values(role=Role.STAFF))
        await db.commit()
    return csrf


async def upload(client, csrf, *, data=None, **values):
    fields = dict(csrf=csrf, title="Тестовый нормативный документ", revision="2026",
                  source_url="https://mgimo.ru/test-document.pdf", reviewed_public="yes")
    fields.update(values)
    return await client.post("/d/documents", data=fields,
        files={"file": ("test.pdf", data if data is not None else pdf_bytes(), "application/pdf")})


async def test_documents_role_and_csrf(client):
    assert (await client.get("/d/documents")).status_code == 303
    csrf = await login(client)
    assert (await client.get("/d/documents")).status_code == 404
    assert (await upload(client, csrf)).status_code == 404
    csrf = await as_staff(client)
    assert (await upload(client, "wrong-token")).status_code == 403
    async with client._transport.app.state.session_factory() as db:
        await db.execute(update(User).where(User.login == "anna").values(role=Role.TEACHER))
        await db.commit()
    assert (await client.get("/d/documents")).status_code == 404
    assert (await upload(client, csrf)).status_code == 404


async def test_upload_validation_and_duplicate(client):
    csrf = await as_staff(client)
    data = pdf_bytes()
    for values in ({"reviewed_public": ""}, {"source_url": "https://evil.test/x.pdf"}, {"title": " "}):
        assert (await upload(client, csrf, data=data, **values)).status_code == 400
    assert (await upload(client, csrf, data=b"not a PDF")).status_code == 400
    first = await upload(client, csrf, data=data)
    second = await upload(client, csrf, data=data)
    assert first.status_code == second.status_code == 303
    assert "uploaded=" in first.headers["location"]
    assert "duplicate=" in second.headers["location"]
    async with client._transport.app.state.session_factory() as db:
        rows = (await db.scalars(select(KnowledgeDocument))).all()
        assert len(rows) == 1 and rows[0].status == Status.QUEUED
        actor = await db.scalar(select(User).where(User.login == "anna"))
        assert rows[0].uploaded_by_user_id == actor.id
        assert rows[0].data == data and rows[0].sha256 == hashlib.sha256(data).hexdigest()
    page = await client.get("/d/documents")
    assert "В очереди" in page.text and page.headers["cache-control"] == "no-store"
    assert "/d/documents" in (await client.get("/d/home")).text


async def test_publication_search_and_idempotent_recovery(client, tmp_path):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    await upload(client, csrf)
    processor = service.DocumentProcessor(app.state.session_factory, app.state.settings)
    assert await processor.process_one()
    target = Path(app.state.settings.rag_corpus_dir) / "chunks.jsonl"
    corpus = Corpus.load(target)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    assert corpus.search("academic leave application", corpus.active(today, today, 30), 5)
    assert all(c.verification_status == "snapshot" and c.reviewed_public for c in corpus.chunks)
    original = target.read_bytes()
    async with app.state.session_factory() as db:
        row = await db.scalar(select(KnowledgeDocument))
        assert row.status == Status.PUBLISHED and row.chunk_count > 0 and row.indexed_at
        row.status = Status.QUEUED  # сбой после публикации файла, но до фиксации результата в БД
        await db.commit()
    await processor.process_one()
    assert target.read_bytes() == original
    from app.assistant.service import Assistant
    from tests.test_assistant import FakeProvider
    cid = corpus.chunks[0].id
    provider = FakeProvider({"status": "answer", "summary_id": cid, "steps": [], "clarification": None},
                            {"accepted_ids": [cid], "complete": True})
    cfg = app.state.settings.model_copy(update={"llm_provider": "gigachat"})
    answer = await Assistant(cfg, provider).ask(1, "academic leave application", [], None)
    assert answer["status"] == "partial" and answer["summary"]["id"] == cid


async def test_published_source_cannot_be_silently_replaced(client, tmp_path):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    await upload(client, csrf)
    processor = service.DocumentProcessor(app.state.session_factory, app.state.settings)
    await processor.process_one()
    original = (tmp_path / "corpus/chunks.jsonl").read_bytes()
    response = await upload(client, csrf, data=pdf_bytes(TEXT + " Updated text."))
    assert response.status_code == 400
    assert (tmp_path / "corpus/chunks.jsonl").read_bytes() == original


async def test_bad_pdf_and_fixed_file_can_be_uploaded(client, tmp_path):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    await upload(client, csrf, data=b"%PDF-broken")
    processor = service.DocumentProcessor(app.state.session_factory, app.state.settings)
    await processor.process_one()
    async with app.state.session_factory() as db:
        row = await db.scalar(select(KnowledgeDocument))
        assert row.status == Status.ERROR
    response = await upload(client, csrf)
    assert response.status_code == 303
    await processor.process_one()
    async with app.state.session_factory() as db:
        assert (await db.scalar(select(KnowledgeDocument))).status == Status.PUBLISHED


async def test_failed_processing_keeps_existing_corpus_and_can_retry(client, tmp_path, monkeypatch):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    processor = service.DocumentProcessor(app.state.session_factory, app.state.settings)
    await upload(client, csrf)
    await processor.process_one()
    target = Path(app.state.settings.rag_corpus_dir) / "chunks.jsonl"
    original = target.read_bytes()
    response = await upload(client, csrf, data=pdf_bytes(TEXT + " Another document."),
                            source_url="https://mgimo.ru/other.pdf")
    assert response.status_code == 303
    real_publish = service.publish_chunks
    monkeypatch.setattr(service, "publish_chunks", lambda *args: (_ for _ in ()).throw(OSError("private-server-path")))
    await processor.process_one()
    assert target.read_bytes() == original
    page = await client.get("/d/documents")
    assert "Повторить обработку" in page.text and "private-server-path" not in page.text
    assert (await client.post("/d/documents/2/retry", data={"csrf": "bad"})).status_code == 403
    assert (await client.post("/d/documents/2/retry", data={"csrf": csrf})).status_code == 303
    monkeypatch.setattr(service, "publish_chunks", real_publish)
    await processor.process_one()
    assert len(Corpus.load(target).chunks) == 2


async def test_oversize_request_without_content_length(client):
    csrf = await as_staff(client)

    async def body():
        yield b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="large.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF-'
        for _ in range(11):
            yield b"x" * 1_000_000
        yield b'\r\n--test-boundary--\r\n'

    response = await client.post("/d/documents", content=body(),
        headers={"X-CSRF-Token": csrf, "Content-Type": "multipart/form-data; boundary=test-boundary"})
    assert response.status_code == 413


async def test_scanned_pdf_needs_review(client, tmp_path):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    await upload(client, csrf, data=pdf_bytes(""))
    await service.DocumentProcessor(app.state.session_factory, app.state.settings).process_one()
    async with app.state.session_factory() as db:
        row = await db.scalar(select(KnowledgeDocument))
        assert row.status == Status.NEEDS_REVIEW and "OCR" in row.error
    assert not (tmp_path / "corpus/chunks.jsonl").exists()
    assert (await upload(client, csrf)).status_code == 303
    await service.DocumentProcessor(app.state.session_factory, app.state.settings).process_one()
    async with app.state.session_factory() as db:
        assert (await db.scalar(select(KnowledgeDocument))).status == Status.PUBLISHED


async def test_restart_recovers_processing_job(client, tmp_path):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    await upload(client, csrf)
    async with app.state.session_factory() as db:
        await db.execute(update(KnowledgeDocument).values(status=Status.PROCESSING))
        await db.commit()
    processor = service.DocumentProcessor(app.state.session_factory, app.state.settings)
    processor.stop.set()
    await REAL_RUN(processor)
    async with app.state.session_factory() as db:
        assert (await db.scalar(select(KnowledgeDocument))).status == Status.QUEUED
    assert await processor.process_one()


def test_atomic_publication_failure_preserves_old_snapshot(tmp_path, monkeypatch):
    from datetime import datetime
    row = KnowledgeDocument(sha256="a"*64, title="Тест", revision="2026",
        source_url="https://mgimo.ru/test.pdf", data=pdf_bytes(), reviewed_public=True,
        uploaded_at=datetime(2026, 11, 16))
    chunks = service.extract_chunks(row)
    service.publish_chunks(tmp_path, chunks)
    original = (tmp_path / "chunks.jsonl").read_bytes()
    other = chunks[0].model_copy(update={"id": "other", "document_id": "other",
        "source_url": "https://mgimo.ru/other.pdf", "source_sha256": "b"*64})
    monkeypatch.setattr(service.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError):
        service.publish_chunks(tmp_path, [other])
    assert (tmp_path / "chunks.jsonl").read_bytes() == original
    assert not list(tmp_path.glob("chunks-upload-*.jsonl"))


def test_document_migration_upgrade_and_downgrade(tmp_path):
    import sqlite3
    from alembic import command
    from alembic.config import Config
    database = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as db:
        assert db.execute("select version_num from alembic_version").fetchone()[0] == "0010_documents"
        assert {r[1] for r in db.execute("pragma table_info(knowledge_documents)")} >= {
            "data", "sha256", "uploaded_by_user_id", "status", "source_key"}
    command.downgrade(config, "0009")
    with sqlite3.connect(database) as db:
        assert not db.execute("select name from sqlite_master where name='knowledge_documents'").fetchone()
    command.upgrade(config, "head")
