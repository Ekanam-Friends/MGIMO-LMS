"""Проверка миграций на выделенном одноразовом PostgreSQL; рабочая БД не допускается."""
import asyncio
import os
from datetime import datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.db.session import make_engine, make_session_factory
from app.generate.run import reset
from app.models import Student
from app.models.auth import User
from app.models.document_notice import DocumentNotice, DocumentRecipient
from app.models.knowledge import KnowledgeDocument

URL = os.environ.get("REVIEW_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="Нужен одноразовый PostgreSQL для ревью")


@pytest.mark.parametrize("previous", ["0009", "0010", "0012_language"])
def test_upgrade_from_either_branch_and_reset_document_recipient(previous):
    # Защита от случайного запуска destructive downgrade на рабочем адресе.
    assert URL == "postgresql+asyncpg://lms_review:review-only-local@127.0.0.1:15478/lms_review"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", URL)
    command.upgrade(cfg, previous)
    command.upgrade(cfg, "head")
    command.check(cfg)

    async def exercise():
        engine = make_engine(URL)
        sf = make_session_factory(engine)
        try:
            async with sf() as db:
                assert await db.scalar(text("select version_num from alembic_version")) == "0013_merge"
                db.add_all([Student(id=1, full_name="Вымышленный студент", record_book="ГЕН-1"),
                            Student(id=2, full_name="Демонстрационный студент", record_book="ДЕМО-2")])
                await db.flush()
                db.add(User(id=1, login="review-staff", password_hash="test-only-hash", role="staff", created_at=datetime(2026, 11, 17)))
                await db.flush()
                db.add(KnowledgeDocument(id=1, uploaded_by_user_id=1, title="Тест", source_url="https://mgimo.ru/test.pdf",
                    source_key="a" * 64, revision="2026", filename="test.pdf", sha256="b" * 64, data=b"%PDF-test",
                    reviewed_public=True, status="published", chunk_count=1, uploaded_at=datetime(2026, 11, 17)))
                await db.flush()
                db.add(DocumentNotice(document_id=1, confirmed_by=1, confirmed_at=datetime(2026, 11, 17)))
                await db.flush()
                db.add_all([DocumentRecipient(document_id=1, student_id=sid, telegram_state="skipped") for sid in (1, 2)])
                await db.commit()
                await reset(db)
                await db.commit()
                assert list((await db.scalars(select(DocumentRecipient.student_id))).all()) == [2]
                assert await db.get(KnowledgeDocument, 1) is not None
        finally:
            await engine.dispose()
    try:
        asyncio.run(exercise())
    finally:
        command.downgrade(cfg, "base")
