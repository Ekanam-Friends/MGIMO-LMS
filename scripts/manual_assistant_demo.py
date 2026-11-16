"""Локальный стенд ручной проверки: только синтетические данные, без внешней LLM/БД.

python -m scripts.manual_assistant_demo
Учётки anna, demo_teacher, demo_staff; пароль из tests.test_web.PASSWORD.
"""
import asyncio
import os
import tempfile
from pathlib import Path

import pytest
import uvicorn

from app.auth.service import hash_password, utcnow
from app.models import Teacher
from app.models.auth import User
from tests.test_web import PASSWORD, client


class DemoProvider:
    async def complete(self, system, data, **kwargs):
        import json
        if "Определи разделы" in system:
            return json.dumps({"scopes":["grades"]})
        if "fact_ids" in system:
            return json.dumps({"fact_ids":[row["id"] for row in data["facts"][:2]]})
        if "Переведи вопрос" in system:
            return json.dumps({"question":"Какие у меня оценки?", "history":data["history"]})
        return json.dumps({"message":"Demo response: check the original data.", "texts":data.get("texts", {})})

    async def aclose(self):
        pass


async def main():
    with tempfile.TemporaryDirectory(prefix="mgimo-ui-") as folder:
        patch = pytest.MonkeyPatch()
        fixture = client.__wrapped__(Path(folder), patch)
        browser_client = await anext(fixture)
        try:
            app = browser_client._transport.app
            app.state.assistant.cfg.assistant_demo_student_access = True
            app.state.assistant.cfg.llm_provider = "gigachat"
            app.state.assistant.provider = DemoProvider()
            async with app.state.session_factory() as db:
                db.add(Teacher(id=1000, ruz_oid=1000, full_name="Тестовый Преподаватель Вымышленный"))
                await db.flush()
                db.add_all([User(login="demo_teacher", password_hash=hash_password(PASSWORD), role="teacher", teacher_id=1000, created_at=utcnow()),
                            User(login="demo_staff", password_hash=hash_password(PASSWORD), role="staff", created_at=utcnow())])
                await db.commit()
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                port=int(os.environ.get("DEMO_PORT", "8011")), lifespan="off"))
            await server.serve()
        finally:
            await fixture.aclose()
            patch.undo()


if __name__ == "__main__":
    asyncio.run(main())
