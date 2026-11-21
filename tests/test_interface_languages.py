import json

import pytest

from app.assistant.conversations import Conversation
from app.assistant.bootstrap import create_assistant as Assistant
from app.assistant.translation import for_display, for_search
from tests.test_assistant import FakeProvider, config
from tests.test_web import client, login


@pytest.mark.parametrize("language", ["en", "fr", "zh"])
async def test_language_saved_in_account_and_bound_to_conversation(client, language):
    csrf = await login(client)
    response = await client.post("/profile/language", data={"language":language,"csrf":csrf})
    assert response.status_code == 303
    page = await client.get("/profile")
    assert f'<html lang="{language}">' in page.text
    assert f'value="{language}" selected' in page.text
    started = await client.post("/s/assistant/conversation", json={"action":"start","acknowledged":True}, headers={"X-CSRF-Token":csrf})
    conversation = client._transport.app.state.assistant.conversations.items[started.json()["conversation_token"]]
    assert conversation.language == language and conversation.student_id == 1
    for route in ["/", "/schedule", "/grades", "/electives", "/mfc/certificates", "/notifications", "/s/assistant", "/services"]:
        assert (await client.get(route)).status_code == 200


async def test_language_requires_csrf_and_rejects_unknown_code(client):
    csrf = await login(client)
    assert (await client.post("/profile/language", data={"language":"en"})).status_code == 403
    assert (await client.post("/profile/language", data={"language":"xx","csrf":csrf})).status_code == 400


@pytest.mark.parametrize("return_to,expected", [
    ("/schedule?week=2026-11-16", "/schedule?week=2026-11-16"),
    ("/", "/"),
    ("https://example.org/", "/profile"),
    ("//example.org/", "/profile"),
    ("/\\example.org/", "/profile"),
    ("/\n/example.org/", "/profile"),
])
async def test_header_language_returns_only_to_local_page(client, return_to, expected):
    csrf = await login(client)
    response = await client.post("/profile/language", data={
        "language": "en", "csrf": csrf, "return_to": return_to,
    })
    assert response.status_code == 303
    assert response.headers["location"] == expected
    assert '<html lang="en">' in (await client.get("/")).text


async def test_translated_question_is_used_for_search_without_answer_history():
    provider = FakeProvider({"question":"Как оформить академический отпуск?","history":["Условия отпуска"]})
    question, history = await for_search(provider, "Comment demander un congé académique ?", ["Conditions"], "fr")
    assert "академический" in question and history == ["Условия отпуска"]
    assert provider.calls[0][1]["interface_language"] == "fr"


async def test_translation_keeps_original_evidence_and_rejects_extra_ids():
    result = {"message":"Проверьте оригинал", "summary":{"text":"Не позднее 10 дней", "url":"https://mgimo.ru/file.pdf"},"steps":[],"facts":[]}
    provider = FakeProvider({"message":"Check the original", "texts":{"summary":"No later than 10 days"}})
    translated = await for_display(provider, result, "en")
    assert translated["summary"]["text"] == "Не позднее 10 дней"
    assert translated["summary"]["translation"] == "No later than 10 days"
    assert translated["summary"]["url"] == "https://mgimo.ru/file.pdf"
    provider = FakeProvider({"message":"Text", "texts":{"invented":"wrong"}})
    with pytest.raises(ValueError):
        await for_display(provider, result, "en")


async def test_foreign_language_grade_request_does_not_read_grades_before_consent(tmp_path, monkeypatch):
    reads = []
    async def collect(*args, **kwargs):
        reads.append(args)
        return []
    monkeypatch.setattr("app.assistant.personal.collect", collect)
    provider = FakeProvider({"question":"Какие у меня оценки?", "history":[]}, {"scopes":["grades"]},
                            {"message":"Allow access to your grades for this conversation?", "texts":{}})
    assistant = Assistant(config(tmp_path), provider)
    conversation = Conversation("session", float("inf"), language="en")
    result = await assistant.chat(1, 1, "What are my grades?", [], None, conversation, None)
    assert result["status"] == "consent_required" and conversation.grades_pending
    assert not reads and result["language"] == "en"


async def test_translation_rejects_changed_deadline_but_accepts_date_format_change():
    result = {"message":"Источник", "summary":{"text":"Не позднее 10 дней, 16.11.2026"}, "steps":[], "facts":[]}
    bad = FakeProvider({"message":"Source", "texts":{"summary":"No later than 30 days, 2026-11-16"}})
    with pytest.raises(ValueError, match="числа"):
        await for_display(bad, result, "en")
    good = FakeProvider({"message":"Source", "texts":{"summary":"No later than 10 days, 2026-11-16"}})
    assert (await for_display(good, result, "en"))["summary"]["translation"].startswith("No later than 10")


async def test_unknown_reference_clarification_uses_interface_language(tmp_path):
    provider = FakeProvider({"question": "Какие условия ее получения?", "history": []},
                            {"scopes": ["public"], "clarification": "О получении чего идёт речь?"},
                            {"message": "Which scholarship or certificate do you mean?", "texts": {}})
    result = await Assistant(config(tmp_path), provider).chat(
        1, 1, "What are the conditions for receiving it?", [], None,
        Conversation("owner", float("inf"), language="en"), None)
    assert result["status"] == "clarify" and result["language"] == "en"
    assert "Which scholarship" in result["message"] and len(provider.calls) == 3
