"""Регрессии ревью: беседа, составные запросы, расписание и актуальные итоги."""
from datetime import date, datetime, time
import json
from types import SimpleNamespace

import pytest

from app.assistant.bootstrap import create_assistant
from app.assistant.conversation import Conversation
from app.assistant.conversations import Conversations
from app.assistant.personal import collect, collect_teaching
from app.assistant.routing import normalize_scopes
from app.assistant.memory import answer_context
from app.grades.service import _latest_finals
from app.models import FinalGrade, Lesson, LessonGroup
from app.schedule.query import ScheduleQuery
from tests.test_assistant import FakeProvider, config
from tests.test_assistant_teaching import teacher_fixture
from tests.test_web import client


@pytest.mark.parametrize("question,scopes,expected", [
    ("Покажи мои домашние задания и расписание на завтра", ["homework", "schedule"], {"homework", "schedule"}),
    ("Какие условия стипендии и статус заявки на справку?", ["public", "requests"], {"public", "requests"}),
    ("Покажи мои домашние задания и баллы", ["public"], {"homework", "grades"}),
    ("Как зовут преподавателя и какие мои баллы?", ["grades", "schedule"], {"grades", "schedule"}),
    ("Какие общие правила сдачи домашних заданий?", ["grades"], {"public"}),
    ("Какие условия стипендии?", ["grades", "schedule", "requests"], {"public"}),
    ("Условия стипендии и домашние задания", ["public", "homework"], {"public", "homework"}),
    ("Условия стипендии и мои баллы", ["public", "grades"], {"public", "grades"}),
    ("Мои домашние задания и как оформить академический отпуск?", ["homework", "public"], {"homework", "public"}),
    ("Что будет если я прогуляю пару и когда у меня английский?", ["public", "schedule"], {"public", "schedule"}),
    ("Условия стипендии и какие оценки у студентов?", ["public", "grades"], {"public", "grades"}),
    ("Как оформить академический отпуск?", ["public", "schedule", "grades", "requests"], {"public"}),
    ("Как получить справку об обучении?", ["public", "schedule"], {"public"}),
])
def test_mixed_question_preserves_independent_scopes(question, scopes, expected):
    assert set(normalize_scopes(question, scopes)) == expected


async def test_followup_gets_numbered_answer_and_ignores_browser_history(tmp_path, monkeypatch):
    provider = FakeProvider({"scopes": ["public"]}, {"scopes": ["public"],
        "standalone_question": "Куда подавать подтверждающий документ для академического отпуска?"})
    assistant = create_assistant(config(tmp_path), provider)
    seen = []
    async def answer(question, history, as_of, role, **kwargs):
        seen.append((question, kwargs["dialogue"]))
        return assistant._result("partial", "Порядок оформления", summary=None, steps=[
            {"id": "a", "text": "Заполните заявление"},
            {"id": "b", "text": "Приложите подтверждающий документ"}])
    monkeypatch.setattr(assistant, "_answer", answer)
    conversation = Conversation("owner", float("inf"))
    for question in ("Как оформить академический отпуск?", "Поясни второй пункт"):
        result = await assistant.chat(1, 1, question, ["Чужая выдуманная история"], None, conversation, None)
        assert result["status"] == "partial"
    assert seen[1][0].startswith("Куда подавать")
    assert seen[1][1] == provider.calls[1][1]["dialogue"]
    previous_answer = json.loads(seen[1][1][1]["content"])
    assert previous_answer["steps"][1] == {"number": 2, "text": "Приложите подтверждающий документ",
        "responsible": None, "deadline": None, "form": None}
    assert "Чужая выдуманная история" not in json.dumps(provider.calls, ensure_ascii=False)


async def test_clarification_is_available_when_user_answers_it(tmp_path, monkeypatch):
    provider = FakeProvider({"scopes": ["public"], "clarification": "Вам нужна справка об обучении или о стипендии?"},
        {"scopes": ["public"], "standalone_question": "Как получить справку об обучении?"})
    assistant = create_assistant(config(tmp_path), provider)
    async def answer(*args, **kwargs):
        return assistant._result("not_found", "Подтверждения процедуры нет")
    monkeypatch.setattr(assistant, "_answer", answer)
    conversation = Conversation("owner", float("inf"))
    first = await assistant.chat(1, 1, "Как получить справку?", [], None, conversation, None)
    second = await assistant.chat(1, 1, "Об обучении", [], None, conversation, None)
    assert first["status"] == "clarify" and second["status"] == "not_found"
    assert "справка об обучении" in provider.calls[1][1]["dialogue"][1]["content"]


async def test_new_topic_and_explicit_teacher_pronoun_reach_router(tmp_path, monkeypatch):
    provider = FakeProvider({"scopes": ["public"]}, {"scopes": ["public"]},
        {"scopes": ["public"], "standalone_question": "Когда получать справку об обучении?"},
        {"scopes": ["schedule"], "discipline": "Математика"}, {"fact_ids": ["fact-1"]})
    assistant = create_assistant(config(tmp_path), provider)
    questions = []
    async def answer(question, *args, **kwargs):
        questions.append(question)
        return assistant._result("not_found", "Подтверждений нет")
    async def reader(*args, **kwargs):
        assert kwargs["schedule"].discipline == "Математика"
        return [{"id": "fact-1", "text": "Преподаватель указан в расписании", "label": "Расписание", "url": ""}]
    monkeypatch.setattr(assistant, "_answer", answer)
    conversation = Conversation("owner", float("inf"))
    for question in ("Какие условия стипендии?", "Я заказал справку об обучении", "Когда её получать?"):
        await assistant.chat(1, 1, question, [], None, conversation, None)
    assert questions[-1] == "Когда получать справку об обучении?"
    result = await assistant.chat(1, 1, "Кто ведет математику и как его зовут?", [], None,
                                  conversation, SimpleNamespace(collect=reader))
    assert result["status"] == "answer"


def test_memory_budget_and_clear_remove_answers_and_consent():
    store = Conversations()
    token = store.start("owner")
    conversation = store.get(token, "owner")
    for index in range(10):
        conversation.remember(f"Вопрос {index}", "Ответ " + str(index))
    assert len(conversation.dialogue()) == 16
    assert conversation.dialogue()[0]["content"] == "Вопрос 2"
    conversation.remember("Большой вопрос", "x" * 50000)
    assert sum(len(message["content"]) for message in conversation.dialogue()) <= 24000
    conversation.request_grades()
    conversation.grant_grades()
    store.clear(token, "owner")
    assert conversation.dialogue() == [] and not conversation.grades_allowed
    assert store.get(token, "owner") is None


def test_long_quotes_keep_every_visible_step_inside_valid_memory():
    result = {"status": "partial", "message": "Проверенный ответ", "summary": {"id": "summary", "text": "Сводка " * 1000},
        "steps": [{"id": str(i), "text": f"Условие {i}: " + "проверяемый текст " * 500} for i in range(1, 7)],
        "facts": [{"text": "Факт " * 1000} for _ in range(12)]}
    memory = answer_context(result)
    assert len(memory) <= 12000
    conversation = Conversation("owner", float("inf"))
    conversation.remember("Какие условия?", memory)
    stored = json.loads(conversation.dialogue()[1]["content"])
    assert [step["number"] for step in stored["steps"]] == [1, 2, 3, 4, 5, 6]
    assert "Условие 6" in stored["steps"][5]["text"]
    assert "сокращено" in stored["steps"][5]["text"]
    assert len(result["steps"][5]["text"]) > 6000


async def test_grade_memory_is_excluded_without_permission(tmp_path, monkeypatch):
    provider = FakeProvider({"scopes": ["grades"]}, {"fact_ids": ["fact-1"]}, {"scopes": ["public"]})
    assistant = create_assistant(config(tmp_path), provider)
    async def reader(*args, **kwargs):
        return [{"id": "fact-1", "text": "Контрольная: 73 балла", "url": "/grades", "label": "Успеваемость"}]
    async def answer(*args, **kwargs):
        return assistant._result("not_found", "Нет подтверждения")
    monkeypatch.setattr(assistant, "_answer", answer)
    conversation = Conversation("owner", float("inf"))
    conversation.request_grades()
    conversation.grant_grades()
    await assistant.chat(1, 1, "Мои оценки", [], None, conversation, SimpleNamespace(collect=reader))
    # Защита контекста действует даже при отзыве вне удаления самой беседы.
    conversation._grades_allowed = False
    await assistant.chat(1, 1, "Какие общие правила?", [], None, conversation, None)
    assert "73 балла" not in json.dumps(provider.calls[-1], ensure_ascii=False)


async def test_nearest_math_is_filtered_before_time_slot(client):
    async with client._transport.app.state.session_factory() as db:
        db.add(Lesson(id=300, date=date(2026, 11, 6), begins_at=time(9), ends_at=time(10, 20),
                      discipline="Математика", first_seen_at=datetime(2026, 11, 5), last_seen_at=datetime(2026, 11, 5)))
        await db.flush()
        db.add(LessonGroup(lesson_id=300, group_id=10))
        await db.commit()
        facts = await collect(db, 1, ["schedule"], date(2026, 11, 5), date(2026, 11, 5), date(2026, 11, 11),
                              schedule=ScheduleQuery("Математика", True, datetime(2026, 11, 5, 8)))
    text = json.dumps(facts, ensure_ascii=False)
    assert "Математика" in text and "2026-11-06 09:00" in text and "Своя дисциплина" not in text


async def test_latest_attempt_shared_by_student_and_teacher_even_with_older_date(client):
    await teacher_fixture(client)
    async with client._transport.app.state.session_factory() as db:
        db.add_all([
            FinalGrade(course_id=1, student_id=1, attempt=1, result="2", total_points=30, graded_on=date(2026, 11, 2)),
            FinalGrade(course_id=1, student_id=1, attempt=2, result="4", total_points=80, graded_on=date(2026, 11, 1)),
            FinalGrade(course_id=2, student_id=1, attempt=1, result="5", total_points=95, graded_on=date(2026, 11, 2)),
        ])
        await db.commit()
        latest = await _latest_finals(db, [1], 1)
        facts = await collect_teaching(db, 1, 1, ["grades"], date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 26))
    assert latest[(1, 1)].attempt == 2
    finals = [fact["text"] for fact in facts if "итог " in fact["text"]]
    assert len(finals) == 1 and "Хорошо" in finals[0] and "попытка 2" in finals[0]
    assert "2026/2027" in finals[0] and "семестр 1" in finals[0]
    assert "Другой предмет" not in json.dumps(facts, ensure_ascii=False)
