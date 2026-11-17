import re

import pytest

from tests.test_web import client, login  # общие синтетические студенты и изолированная БД


async def start_chat(client, csrf):
    response = await client.post("/s/assistant/conversation", json={"action":"start", "acknowledged":True},
                                headers={"X-CSRF-Token":csrf})
    assert response.status_code == 200
    return response.json()["conversation_token"]


async def test_assistant_requires_login_and_csrf(client):
    assert (await client.get("/s/assistant")).status_code == 303
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск"})).status_code == 303
    csrf = await login(client)
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск"})).status_code == 403
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск"}, headers={"X-CSRF-Token":csrf})).status_code == 403
    token = await start_chat(client, csrf)
    result = await client.post("/s/assistant/ask", json={"question":"Академический отпуск", "conversation_token":token}, headers={"X-CSRF-Token":csrf})
    assert result.status_code == 200 and result.json()["status"] == "unavailable"
    assert result.headers["cache-control"] == "no-store"


async def test_assistant_page_and_payload_limits(client):
    csrf = await login(client)
    page = await client.get("/s/assistant")
    assert "GigaChat" in page.text and "Разберёмся" in page.text
    assert "секрет Бориса" not in page.text
    headers = {"X-CSRF-Token":csrf}
    for payload in [{"question":"x"}, {"question":"x"*2001}, {"question":"Вопрос", "student_id":2},
                    {"question":"Вопрос", "history":["x"]*5}, {"question":"Вопрос", "as_of":"bad"}]:
        assert (await client.post("/s/assistant/ask", json=payload, headers=headers)).status_code == 400
    assert (await client.post("/s/assistant/ask", json={"question":"x"*25000}, headers=headers)).status_code == 413


async def test_staff_public_chat_and_demo_access_disabled_by_default(client):
    from sqlalchemy import update
    from app.models.auth import User, Role
    csrf = await login(client)
    async with client._transport.app.state.session_factory() as db:
        await db.execute(update(User).where(User.login == "anna").values(role=Role.STAFF))
        await db.commit()
    assert (await client.get("/s/assistant")).status_code == 200
    token = await start_chat(client, csrf)
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск", "conversation_token":token},
                             headers={"X-CSRF-Token":csrf})).status_code == 200
    assert (await client.get("/s/assistant/students")).status_code == 404
    assert (await client.post("/s/assistant/conversation", json={"action":"start", "acknowledged":True, "student_id":2},
                             headers={"X-CSRF-Token":csrf})).status_code == 403


@pytest.mark.parametrize("role", ["staff"])
async def test_demo_roles_select_any_student_and_consent_is_bound(client, role):
    from sqlalchemy import update
    from app.models.auth import User
    csrf = await login(client)
    app = client._transport.app
    app.state.assistant.cfg.assistant_demo_student_access = True
    async with app.state.session_factory() as db:
        await db.execute(update(User).where(User.login == "anna").values(role=role, student_id=None))
        await db.commit()
    assert (await client.get("/s/assistant")).status_code == 200
    students = (await client.get("/s/assistant/students?q=Борис")).json()["students"]
    assert [s["id"] for s in students] == [2]
    headers = {"X-CSRF-Token":csrf}
    response = await client.post("/s/assistant/conversation", json={"action":"start", "acknowledged":True, "student_id":2}, headers=headers)
    token = response.json()["conversation_token"]
    conversation = app.state.assistant.conversations.items[token]
    assert conversation.student_id == 2 and conversation.actor_role == role
    conversation.request_grades()
    assert (await client.post("/s/assistant/conversation", json={"action":"allow_grades", "conversation_token":token}, headers=headers)).status_code == 200
    assert conversation.grades_allowed
    other = (await client.post("/s/assistant/conversation", json={"action":"start", "acknowledged":True, "student_id":1}, headers=headers)).json()["conversation_token"]
    assert not app.state.assistant.conversations.items[other].grades_allowed
    app.state.assistant.cfg.assistant_demo_student_access = False
    assert (await client.post("/s/assistant/ask", json={"question":"Какие оценки", "conversation_token":token}, headers=headers)).status_code == 403


async def test_student_cannot_select_another_student_even_in_demo(client):
    csrf = await login(client)
    client._transport.app.state.assistant.cfg.assistant_demo_student_access = True
    assert (await client.get("/s/assistant/students")).status_code == 404
    response = await client.post("/s/assistant/conversation", json={"action":"start", "acknowledged":True, "student_id":2}, headers={"X-CSRF-Token":csrf})
    assert response.status_code == 404


async def test_assistant_uses_user_from_session_and_does_not_send_profile(client):
    from app.assistant.service import BusyError
    csrf = await login(client)
    token = await start_chat(client, csrf)
    seen = []
    class Recorder:
        async def chat(self, *args):
            seen.append(args)
            if len(seen) > 1:
                raise BusyError()
            return {"status":"not_found", "message":"Тест"}
    app = client._transport.app
    original = app.state.assistant
    app.state.assistant = Recorder()
    app.state.assistant.conversations = original.conversations
    try:
        response = await client.post("/s/assistant/ask", json={"question":"Вопрос по положениям", "conversation_token":token}, headers={"X-CSRF-Token":csrf})
        assert response.status_code == 200 and len(seen[0]) == 7
        assert type(seen[0][0]) is int
        assert seen[0][1:5] == (1, "Вопрос по положениям", [], None)
        response = await client.post("/s/assistant/ask", json={"question":"Вопрос по положениям", "conversation_token":token}, headers={"X-CSRF-Token":csrf})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
    finally:
        app.state.assistant = original
