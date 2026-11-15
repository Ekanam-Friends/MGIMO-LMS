import re

import pytest

from tests.test_web import client, login  # общие синтетические студенты и изолированная БД


async def test_assistant_requires_login_and_csrf(client):
    assert (await client.get("/s/assistant")).status_code == 303
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск"})).status_code == 303
    csrf = await login(client)
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск"})).status_code == 403
    result = await client.post("/s/assistant/ask", json={"question":"Академический отпуск"}, headers={"X-CSRF-Token":csrf})
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


async def test_staff_cannot_use_student_assistant(client):
    from sqlalchemy import update
    from app.models.auth import User, Role
    csrf = await login(client)
    async with client._transport.app.state.session_factory() as db:
        await db.execute(update(User).where(User.login == "anna").values(role=Role.STAFF))
        await db.commit()
    assert (await client.get("/s/assistant")).status_code == 404
    assert (await client.post("/s/assistant/ask", json={"question":"Академический отпуск"},
                             headers={"X-CSRF-Token":csrf})).status_code == 404


async def test_assistant_uses_user_from_session_and_does_not_send_profile(client):
    from app.assistant.service import BusyError
    csrf = await login(client)
    seen = []
    class Recorder:
        async def ask(self, *args):
            seen.append(args)
            if len(seen) > 1:
                raise BusyError()
            return {"status":"not_found", "message":"Тест"}
    app = client._transport.app
    original = app.state.assistant
    app.state.assistant = Recorder()
    try:
        response = await client.post("/s/assistant/ask", json={"question":"Вопрос по положениям"}, headers={"X-CSRF-Token":csrf})
        assert response.status_code == 200 and len(seen[0]) == 4
        assert type(seen[0][0]) is int and seen[0][1:] == ("Вопрос по положениям", [], None)
        response = await client.post("/s/assistant/ask", json={"question":"Вопрос по положениям"}, headers={"X-CSRF-Token":csrf})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"
    finally:
        app.state.assistant = original
