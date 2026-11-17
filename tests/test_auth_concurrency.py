"""Дорогая проверка пароля не останавливает обработку остальных запросов."""
import asyncio
import threading

from app.auth import service
from tests.test_web import PASSWORD, client


async def test_slow_password_verification_keeps_event_loop_available(client, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original = service._check_password

    def slow_check(encoded, password):
        started.set()
        release.wait(2)
        return original(encoded, password)

    monkeypatch.setattr(service, "_check_password", slow_check)
    task = asyncio.create_task(client.post("/login", data={"login": "anna", "password": PASSWORD}))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        assert not release.is_set() and not task.done()
        response = await asyncio.wait_for(client.get("/healthz"), .5)
        assert response.status_code == 200 and not task.done()
    finally:
        release.set()
        response = await task
    assert response.status_code == 303
