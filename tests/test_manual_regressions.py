"""Регрессии реальных мобильных сценариев, с изоляцией SQL и отказами провайдера."""
import json
from datetime import date, datetime, time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event

from app.assistant.bootstrap import create_assistant
from app.assistant.conversation import Conversation
from app.assistant.errors import ProviderError
from app.assistant.personal import collect
from app.schedule.query import ScheduleQuery
from app.assistant.provider import GigaChat
from app.models import (Assessment, Course, CourseEnrollment, Discipline, ElectiveChoice,
                        Lesson, LessonGroup, Teacher, lesson_teachers)
from tests.test_assistant import FakeProvider, config
from tests.test_web import client


async def test_thanks_does_not_restart_or_call_provider(tmp_path):
    p = FakeProvider()
    result = await create_assistant(config(tmp_path), p).chat(
        1, 1, "Спасибо!", [], None, Conversation("owner", float("inf")), None)
    assert result["status"] == "help" and "Пожалуйста" in result["message"]
    assert "Здравствуйте" not in result["message"] and not p.calls


async def test_failed_question_keeps_topic_on_server(tmp_path, monkeypatch):
    p = FakeProvider({"scopes": ["public"]}, {"scopes": ["grades"],
        "standalone_question": "Какие условия получения стипендии?", "search_query": "стипендия условия"})
    assistant = create_assistant(config(tmp_path), p)
    async def public(*args, **kwargs):
        return assistant._result("not_found", "Подтверждений нет")
    monkeypatch.setattr(assistant, "_answer", public)
    c = Conversation("owner", float("inf"))
    first = await assistant.chat(1, 1, "А что по стипухе?", [], None, c, None)
    second = await assistant.chat(1, 1, "Какие условия ее получения?", [], None, c, None)
    assert first["status"] == second["status"] == "not_found"
    assert c.history == ["А что по стипухе?", "Какие условия ее получения?"]
    assert p.calls[1][1]["question"] == "Какие условия ее получения?"
    assert "Подтверждений нет" in p.calls[1][1]["dialogue"][1]["content"]
    assert not c.grades_pending


async def test_unknown_reference_asks_instead_of_inventing_procedure(tmp_path):
    p = FakeProvider({"scopes": ["public"], "clarification": "Уточните, о получении чего идёт речь?"})
    r = await create_assistant(config(tmp_path), p).chat(
        1, 1, "Какие условия ее получения?", [], None, Conversation("owner", float("inf")), None)
    assert r["status"] == "clarify" and len(p.calls) == 1


@pytest.mark.parametrize("question", ["Какие условия получения повышенной стипендии?",
                                      "Что будет, если я прогуляю пару?"])
async def test_public_rule_cannot_read_grades_on_bad_route(tmp_path, monkeypatch, question):
    p = FakeProvider({"scopes": ["grades", "schedule", "requests"]})
    assistant = create_assistant(config(tmp_path), p)
    async def public(*args, **kwargs):
        return assistant._result("partial", "Условия по источникам")
    async def forbidden(*args):
        pytest.fail("Общий нормативный вопрос не должен читать кабинет")
    monkeypatch.setattr(assistant, "_answer", public)
    c = Conversation("owner", float("inf"))
    r = await assistant.chat(1, 1, question, [], None, c, SimpleNamespace(collect=forbidden))
    assert r["status"] == "partial" and not c.grades_pending


async def test_personal_scholarship_still_requires_consent(tmp_path):
    p = FakeProvider({"scopes": ["public", "grades"]})
    c = Conversation("owner", float("inf"))
    r = await create_assistant(config(tmp_path), p).chat(
        1, 1, "Положена ли мне повышенная стипендия по моим оценкам?", [], None, c, None)
    assert r["status"] == "consent_required" and c.grades_pending


async def test_homework_without_score_is_visible_and_foreign_course_excluded(client):
    async with client._transport.app.state.session_factory() as db:
        db.add_all([Discipline(id=1, name="Тестовый курс"), Discipline(id=2, name="Чужой курс")])
        await db.flush()
        db.add_all([Course(id=i, discipline_id=i, group_id=i * 10, semester=1,
                          academic_year="2026/2027", created_at=datetime(2026, 9, 1)) for i in (1, 2)])
        await db.flush()
        db.add_all([CourseEnrollment(course_id=i, student_id=i) for i in (1, 2)])
        db.add_all([Assessment(course_id=i, title=f"Задание {i}", description=f"Описание {i}",
                               due_date=date(2026, 11, 25), created_at=datetime(2026, 11, 19)) for i in (1, 2)])
        await db.commit()
        queries = []
        def capture(conn, cursor, statement, parameters, context, many):
            queries.append(statement.lower())
        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            facts = await collect(db, 1, ["homework"], date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 26))
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    data = json.dumps(facts, ensure_ascii=False)
    assert "Задание 1" in data and "Описание 1" in data and "2026-11-25" in data
    assert "Задание 2" not in data and "Чужой курс" not in data
    assert not any("from scores" in q or "join scores" in q or "final_grades" in q for q in queries)


async def test_schedule_uses_saved_subgroup_and_teacher(client):
    async with client._transport.app.state.session_factory() as db:
        db.add(Teacher(id=1, ruz_oid=1, full_name="Примерова Тестина Вымышленовна"))
        db.add(ElectiveChoice(student_id=1, kind="subgroup", value="01-англ (1ТЕСТ-01)"))
        await db.flush()
        await db.execute(lesson_teachers.insert().values(lesson_id=101, teacher_id=1))
        await db.commit()
        facts = await collect(db, 1, ["schedule"], date(2026, 11, 5), date(2026, 11, 5), date(2026, 11, 5),
                              schedule=ScheduleQuery(discipline="английск"))
    data = json.dumps(facts, ensure_ascii=False)
    assert "Примерова Тестина" in data and "01-англ" in data
    assert "02-нем" not in data and "Чужая дисциплина" not in data
    assert "active" not in data and "T00:00" not in data


async def test_nearest_schedule_omits_finished_lesson(client, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 5, 18, 30, tzinfo=tz)
    async with client._transport.app.state.session_factory() as db:
        db.add(ElectiveChoice(student_id=1, kind="subgroup", value="01-англ (1ТЕСТ-01)"))
        db.add(Lesson(id=300, date=date(2026, 11, 6), begins_at=time(9), ends_at=time(10, 20),
                      discipline="английский язык", subgroup="01-англ (1ТЕСТ-01)",
                      first_seen_at=datetime(2026, 11, 5), last_seen_at=datetime(2026, 11, 5)))
        await db.flush()
        db.add(Lesson(id=301, date=date(2026, 11, 7), begins_at=time(9), ends_at=time(10, 20),
                      discipline="английский язык", subgroup="01-англ (1ТЕСТ-01)",
                      first_seen_at=datetime(2026, 11, 5), last_seen_at=datetime(2026, 11, 5)))
        await db.flush()
        db.add_all([LessonGroup(lesson_id=i, group_id=10) for i in (300, 301)])
        await db.commit()
        facts = await collect(db, 1, ["schedule"], date(2026, 11, 5), date(2026, 11, 5), date(2026, 11, 11),
                              schedule=ScheduleQuery(discipline="английск", nearest=True, now=Clock.now()))
    text = json.dumps(facts, ensure_ascii=False)
    assert "2026-11-06 09:00" in text and "2026-11-05 10:35" not in text
    assert "2026-11-07 09:00" not in text


def test_ocr_numeric_subitem_remains_inside_parent_clause():
    from scripts.rag.publish_snapshot import fragments
    from app.assistant.excerpts import document_excerpts
    pages = ["43. Повышенная стипендия назначается при соблюдении критериев.\nа) Отличные оценки за два семестра;",
             "6) Награда за проектную деятельность;\nв) Научная публикация за год.\n44. Дополнительные ограничения применяются при пересдаче экзаменов."]
    published = list(fragments([{"text": p} for p in pages]))
    for rows in (published, [(r.text, r.page, r.page_end) for r in document_excerpts(pages)]):
        first = next(r for r in rows if "43." in r[0])
        assert "6)" in first[0] and "в)" in first[0] and first[1:] == (1, 2)
        assert "44." not in first[0]


@pytest.mark.parametrize("status", [429, 503])
async def test_transient_provider_error_retried_once(status, monkeypatch):
    calls = []
    async def no_wait(seconds):
        pass
    monkeypatch.setattr("app.assistant.provider.asyncio.sleep", no_wait)
    def handler(request):
        if request.url.path.endswith("oauth"):
            return httpx.Response(200, json={"access_token": "test-secret", "expires_at": 9999999999999})
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": "1"}, text="private-body")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]})
    p = GigaChat("test-key", "scope", "model", transport=httpx.MockTransport(handler))
    try:
        assert await p.complete("system", {}) == "{}" and len(calls) == 2
    finally:
        await p.aclose()


async def test_long_retry_after_is_not_shortened():
    calls = []
    def handler(request):
        if request.url.path.endswith("oauth"):
            return httpx.Response(200, json={"access_token": "test-secret", "expires_at": 9999999999999})
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, text="private-body")
    p = GigaChat("test-key", "scope", "model", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as err:
            await p.complete("system", {})
        assert err.value.code == "http_429" and len(calls) == 1
        assert "private-body" not in str(err.value) and "test-secret" not in str(err.value)
    finally:
        await p.aclose()


@pytest.mark.parametrize("finish,code", [("length", "generation_length"), ("blacklist", "generation_refused")])
async def test_generation_failure_is_distinguished_and_not_retried(finish, code):
    calls = []
    def handler(request):
        if request.url.path.endswith("oauth"):
            return httpx.Response(200, json={"access_token": "test-secret", "expires_at": 9999999999999})
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": "private-body"}}]})
    p = GigaChat("test-key", "scope", "model", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderError) as err:
            await p.complete("system", {})
        assert err.value.code == code and len(calls) == 1
    finally:
        await p.aclose()


async def test_error_logs_contain_reason_and_id_without_secret(tmp_path, caplog):
    p = FakeProvider(ProviderError("secret question and credentials", code="generation_refused"))
    r = await create_assistant(config(tmp_path), p).chat(
        1, 1, "Что будет, если пропустить пару?", [], None, Conversation("owner", float("inf")), None)
    assert r["status"] == "error" and "отклонил" in r["message"]
    assert r["request_id"] in caplog.text and "generation_refused" in caplog.text
    assert "secret question" not in caplog.text + json.dumps(r)


def test_colloquial_scholarship_and_absence_are_found_without_llm_rewrite():
    from app.assistant.corpus import Corpus
    from tests.test_assistant import chunk
    stipend = chunk(id="stipend", title="Стипендиальное обеспечение", text="Стипендия назначается за достижения студента.")
    absence = chunk(id="absence", document_id="attendance", title="Посещаемость", text="Пропуск занятий без уважительной причины учитывается отдельно.")
    corpus = Corpus([stipend, absence], "test")
    assert corpus.search("А что по стипухе?", corpus.chunks, 1)[0].id == "stipend"
    assert corpus.search("Что будет, если я прогуляю пару?", corpus.chunks, 1)[0].id == "absence"


def test_homework_rules_stay_public_and_personal_tasks_stay_separate():
    from app.assistant.routing import normalize_scopes
    assert normalize_scopes("Какие общие правила сдачи домашних заданий?", ["grades"]) == ["public"]
    assert normalize_scopes("Покажи мои домашние задания и баллы", ["public"]) == ["homework", "grades"]
