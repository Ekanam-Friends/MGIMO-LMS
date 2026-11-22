"""Регрессии ревью #38: миграции, нормативные исключения и ограничение контекста."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.assistant.bootstrap import create_assistant
from app.assistant.conversations import Conversation
from app.assistant.documents import extract_chunks
from app.assistant.evidence import bounded_facts
from app.assistant.excerpts import document_excerpts, ExcerptTooLong
from app.models.knowledge import KnowledgeDocument
from tests.test_assistant import FakeProvider, config


@pytest.mark.parametrize("previous", ["0009", "0010", "0012_language"])
def test_migrations_have_one_head_and_upgrade_either_deployed_branch(tmp_path, previous):
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}")
    assert ScriptDirectory.from_config(cfg).get_heads() == ["0015_learning_delivery"]
    command.upgrade(cfg, previous)
    command.upgrade(cfg, "head")
    import sqlite3
    with sqlite3.connect(tmp_path / 'upgrade.db') as db:
        assert db.execute("select version_num from alembic_version").fetchall() == [("0015_learning_delivery",)]
        assert db.execute("select count(*) from group_schedule_coverage").fetchone() == (0,)
        assert db.execute("select count(*) from knowledge_documents").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("insert into users (login,password_hash,role,created_at,interface_language) values ('test','test','staff','2026-11-17','xx')")
    command.downgrade(cfg, "0009")
    command.upgrade(cfg, "head")


def test_uploaded_pdf_keeps_exception_on_next_page():
    import pymupdf
    pages = ["1. Students submit an application to the dean office to request academic leave.",
             "Except students who have already completed this procedure and received the final approval."]
    with pymupdf.open() as pdf:
        for text in pages:
            pdf.new_page().insert_textbox((50, 50, 500, 700), text)
        data = pdf.tobytes()
    row = KnowledgeDocument(sha256="a" * 64, title="Тест", revision="2026", data=data,
        source_url="https://mgimo.ru/test.pdf", uploaded_at=datetime(2026, 11, 17), reviewed_public=True)
    chunks = extract_chunks(row)
    assert len(chunks) == 1
    assert "Except students" in chunks[0].text
    assert (chunks[0].page, chunks[0].page_end) == (1, 2)


def test_long_clause_requires_review_instead_of_cutting_condition():
    with pytest.raises(ExcerptTooLong):
        document_excerpts(["1. " + "Condition and exception. " * 400])


def test_grade_history_does_not_displace_requests_or_schedule():
    facts = [{"id": f"grade-{i}", "text": "Баллы", "url": "/grades", "label": "Успеваемость"} for i in range(180)]
    facts += [{"id": "request-1", "text": "Заявка", "url": "/mfc/certificates/1", "label": "Заявки"},
              {"id": "lesson-1", "text": "Занятие", "url": "/schedule", "label": "Расписание группы"}]
    result = bounded_facts(facts)
    assert len(result) == 160
    assert {"request-1", "lesson-1", "fact-limit"} <= {r["id"] for r in result}


async def test_model_cannot_hide_context_limit(tmp_path):
    async def collect(*args, **kwargs):
        return [{"id": "a", "text": "Факт", "url": "/grades", "label": "Успеваемость"},
                {"id": "fact-limit", "text": "Есть пропуски", "url": "", "label": "Ограничение выборки"}]
    provider = FakeProvider({"scopes": ["grades"]}, {"fact_ids": ["a"]})
    conversation = Conversation("session", float("inf"))
    conversation.request_grades()
    conversation.grant_grades()
    result = await create_assistant(config(tmp_path), provider).chat(
        1, 1, "Мои оценки", [], None, conversation, SimpleNamespace(collect=collect))
    assert result["status"] == "partial"
    assert any(f["id"] == "fact-limit" for f in result["facts"])


def test_grade_consent_cannot_be_granted_without_request():
    conversation = Conversation("session", float("inf"))
    with pytest.raises(ValueError):
        conversation.grant_grades()
    with pytest.raises(AttributeError):
        conversation.grades_allowed = True


@pytest.mark.parametrize(("question", "route", "expected"), [
    ("Каков порядок публикации объявлений?", ["public"], ["public"]),
    ("Какие правила публикации объявлений?", ["notifications"], ["public"]),
    ("Что мне написал преподаватель?", ["public"], ["public", "notifications"]),
    ("Каков порядок публикации объявлений и какие у меня уведомления?", ["public"], ["public", "notifications"]),
])
def test_notification_scopes_distinguish_public_rules_and_personal_feed(question, route, expected):
    from app.assistant.routing import normalize_scopes

    assert normalize_scopes(question, route) == expected
