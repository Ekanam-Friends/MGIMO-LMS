"""Разрешения и изоляция личных данных: реальные запросы SQLite, LLM заменена."""
from datetime import date
from types import SimpleNamespace
import json

import pytest

from app.assistant.conversations import Conversation, Conversations
from app.assistant.personal import collect
from app.assistant.bootstrap import create_assistant as Assistant
from tests.test_assistant import FakeProvider, config
from tests.test_assistant_web import start_chat
from tests.test_web import client, login


def test_conversation_owner_expiry_and_clear(monkeypatch):
    monkeypatch.setattr("app.assistant.conversations.time.monotonic", lambda: 10)
    store = Conversations()
    token = store.start("session-a")
    assert store.get(token, "session-b") is None
    store.get(token, "session-a").request_grades()
    store.get(token, "session-a").grant_grades()
    store.clear(token, "session-a")
    assert store.get(token, "session-a") is None
    token = store.start("session-a")
    assert not store.get(token, "session-a").grades_allowed
    monkeypatch.setattr("app.assistant.conversations.time.monotonic", lambda: 7211)
    assert store.get(token, "session-a") is None


def test_snapshot_fragments_preserve_cross_page_clause():
    from scripts.rag.publish_snapshot import fragments
    pages = [{"text":"1.1. Для оформления требуется письменное заявление студента и"},
             {"text":"документ, подтверждающий обстоятельства.\n2. Следующий пункт применяется отдельно."}]
    rows = list(fragments(pages))
    first = next(row for row in rows if "письменное" in row[0])
    assert "подтверждающий" in first[0] and first[1:] == (1,2)


async def test_no_grade_read_or_transfer_before_permission(tmp_path, monkeypatch):
    reads = []
    async def collector(*args, **kwargs):
        reads.append(args)
        return [{"id":"fact-1", "text":"Тест: 75 баллов", "url":"/grades", "label":"Успеваемость"}]
    monkeypatch.setattr("app.assistant.personal.collect", collector)
    provider = FakeProvider({"scopes":["grades"]}, {"scopes":["grades"]}, {"fact_ids":["fact-1"]})
    assistant = Assistant(config(tmp_path), provider)
    conversation = Conversation("session", float("inf"))
    first = await assistant.chat(1, 1, "Какие у меня оценки?", [], None, conversation, SimpleNamespace(collect=collector))
    assert first["status"] == "consent_required" and conversation.grades_pending
    assert not reads and "75" not in json.dumps(provider.calls)
    conversation.request_grades()
    conversation.grant_grades()
    second = await assistant.chat(1, 1, "Какие у меня оценки?", [], None, conversation, SimpleNamespace(collect=collector))
    assert len(reads) == 1 and second["facts"][0]["text"] == "Тест: 75 баллов"


async def test_session_admission_requests_discipline_before_reading_grades(tmp_path):
    provider = FakeProvider()
    assistant = Assistant(config(tmp_path), provider)
    conversation = Conversation("session", float("inf"))
    answer = await assistant.chat(1, 1, "Есть ли у меня допуск к аессии?", [], None, conversation, None)
    assert answer["status"] == "clarify" and answer["section"] == "grades"
    assert not conversation.grades_pending and not provider.calls


async def test_acknowledgement_and_grant_require_explicit_server_flow(client):
    csrf = await login(client)
    headers = {"X-CSRF-Token":csrf}
    assert (await client.post("/s/assistant/conversation", json={"action":"start"}, headers=headers)).status_code == 400
    token = await start_chat(client, csrf)
    payload = {"action":"allow_grades", "conversation_token":token}
    assert (await client.post("/s/assistant/conversation", json=payload, headers=headers)).status_code == 400
    store = client._transport.app.state.assistant.conversations
    store.items[token].request_grades()
    assert (await client.post("/s/assistant/conversation", json=payload, headers=headers)).status_code == 200
    assert store.items[token].grades_allowed
    assert (await client.post("/s/assistant/conversation", json={"action":"clear", "conversation_token":token}, headers=headers)).status_code == 200
    assert token not in store.items
    assert (await client.post("/s/assistant/ask", json={"question":"Мои оценки", "conversation_token":token}, headers=headers)).status_code == 403
    new = await start_chat(client, csrf)
    assert not store.items[new].grades_allowed
    other_csrf = await login(client, "boris")
    assert (await client.post("/s/assistant/ask", json={"question":"Мои оценки", "conversation_token":new}, headers={"X-CSRF-Token":other_csrf})).status_code == 403


async def test_personal_data_filters_student_in_queries(client):
    async with client._transport.app.state.session_factory() as db:
        facts = await collect(db, 1, ["schedule", "requests"], date(2026,11,5), date(2026,11,5), date(2026,11,5))
    text = json.dumps(facts, ensure_ascii=False)
    assert "Своя дисциплина" in text
    assert "Чужая дисциплина" not in text and "Борис" not in text and "секрет" not in text
    assert not any(f["url"] == f"/mfc/certificates/{client.boris_request_id}" for f in facts)


async def test_grades_only_include_current_student(client):
    from datetime import datetime
    from app.models import Assessment, Course, CourseEnrollment, Discipline, Score
    async with client._transport.app.state.session_factory() as db:
        db.add_all([Discipline(id=1, name="Математика"), Discipline(id=2, name="Чужой предмет")])
        await db.flush()
        db.add_all([Course(id=1, discipline_id=1, group_id=10, semester=1, academic_year="2026/2027", created_at=datetime(2026,9,1)),
                    Course(id=2, discipline_id=2, group_id=20, semester=1, academic_year="2026/2027", created_at=datetime(2026,9,1))])
        await db.flush()
        db.add_all([CourseEnrollment(course_id=1, student_id=1), CourseEnrollment(course_id=2, student_id=2),
                    Assessment(id=1, course_id=1, title="Тест", max_points=10, created_at=datetime(2026,9,1)),
                    Assessment(id=2, course_id=2, title="Чужой тест", max_points=10, created_at=datetime(2026,9,1))])
        await db.flush()
        db.add_all([Score(assessment_id=1, student_id=1, points=7, graded_at=datetime(2026,11,1)),
                    Score(assessment_id=2, student_id=2, points=9, graded_at=datetime(2026,11,1))])
        await db.commit()
        facts = await collect(db, 1, ["grades"], date(2026,11,16), date(2026,11,16), date(2026,11,16))
    text = json.dumps(facts, ensure_ascii=False)
    assert "7 из 10" in text and "Математика" in text
    assert "Чужой" not in text and "9 из 10" not in text


async def test_invented_personal_evidence_fails_closed(tmp_path, monkeypatch):
    async def collector(*args, **kwargs):
        return [{"id":"fact-1", "text":"Реальная запись", "url":"/grades", "label":"Успеваемость"}]
    monkeypatch.setattr("app.assistant.personal.collect", collector)
    provider = FakeProvider({"scopes":["grades"]}, {"fact_ids":["invented"]})
    conversation = Conversation("session", float("inf"))
    conversation.request_grades()
    conversation.grant_grades()
    result = await Assistant(config(tmp_path), provider).chat(1,1,"Мои оценки",[],None,conversation,SimpleNamespace(collect=collector))
    assert result["status"] == "error" and not result.get("facts")
