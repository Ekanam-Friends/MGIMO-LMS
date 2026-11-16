"""ИИ только советует; работник подтверждает адресатов, доставка проверяется заглушками."""
import json
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from app.assistant.documents import DocumentProcessor
from app.assistant.provider import ProviderError
from app.auth.service import utcnow
from app.bot.document_notify import deliver_document_notifications
from app.models import Student, TelegramLink
from app.models.auth import Role, User
from app.models.document_notice import DocumentNotice, DocumentRecipient
from app.notifications import documents as service
from tests.test_document_upload import client, as_staff, upload, pause_background
from tests.test_web import PASSWORD


async def prepared(client, tmp_path):
    csrf = await as_staff(client)
    app = client._transport.app
    app.state.settings.rag_corpus_dir = str(tmp_path / "corpus")
    await upload(client, csrf)
    await DocumentProcessor(app.state.session_factory, app.state.settings).process_one()
    return csrf, app


class Provider:
    def __init__(self, targets=None, error=False):
        self.calls = []
        self.targets = targets if targets is not None else ["group:20"]
        self.error = error

    async def complete(self, system, payload):
        self.calls.append(payload)
        if self.error:
            raise ProviderError("unavailable")
        return json.dumps({"targets": self.targets, "explanation": "Документ касается выбранной группы. Проверьте область применения."})

    async def aclose(self):
        pass


async def preview(client, csrf, targets=None):
    return await client.post("/d/documents/1/notify/preview", data={"csrf": csrf, "targets": targets or ["group:20"]})


async def confirm(client, csrf, app):
    async with app.state.session_factory() as db:
        notice = await db.get(DocumentNotice, 1)
        token = notice.preview_token if notice else ""
    return await client.post("/d/documents/1/notify/confirm", data={"csrf": csrf, "preview_token": token})


async def test_ai_suggestion_never_sends_and_omits_personal_data(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    provider = Provider()
    app.state.assistant.provider = provider
    response = await client.post("/d/documents/1/notify/suggest", data={"csrf": csrf})
    assert response.status_code == 303
    async with app.state.session_factory() as db:
        notice = await db.get(DocumentNotice, 1)
        assert notice.suggested_targets == ["group:20"] and notice.confirmed_at is None
        assert not (await db.scalars(select(DocumentRecipient))).all()
    sent = json.dumps(provider.calls, ensure_ascii=False)
    assert "Борис" not in sent and "Анна" not in sent
    assert "record_book" not in sent and "telegram_id" not in sent
    page = await client.get("/d/documents/1/notify")
    assert 'value="group:20" checked' in page.text and "Проверьте область применения" in page.text
    assert (await client.post("/d/documents/1/notify/suggest", data={"csrf": csrf})).status_code == 400


async def test_manual_fallback_confirm_and_recipient_isolation(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    assert (await client.post("/d/documents/1/notify/suggest", data={"csrf": csrf})).status_code == 400
    assert (await client.post("/d/documents/1/notify/confirm", data={"csrf": csrf})).status_code == 400
    response = await preview(client, csrf)
    assert response.status_code == 200 and "Получателей: 1" in response.text
    assert (await confirm(client, csrf, app)).status_code == 303
    assert (await confirm(client, csrf, app)).status_code == 303
    async with app.state.session_factory() as db:
        rows = (await db.scalars(select(DocumentRecipient))).all()
        assert len(rows) == 1 and rows[0].student_id == 2
        assert rows[0].telegram_state == "skipped"  # бот не подключён
    await client.post("/login", data={"login": "boris", "password": PASSWORD})
    assert "Новый документ:" in (await client.get("/notifications")).text
    assert "Новый документ:" in (await client.get("/notifications/panel")).text
    async with app.state.session_factory() as db:
        await db.execute(update(User).where(User.login == "anna").values(role=Role.STUDENT))
        await db.commit()
    await client.post("/login", data={"login": "anna", "password": PASSWORD})
    assert "Новый документ:" not in (await client.get("/notifications")).text
    assert (await client.get("/d/documents/1/notify")).status_code == 404
    assert (await client.post("/d/documents/1/notify/confirm", data={"csrf": csrf})).status_code in (403, 404)


async def test_wrong_csrf_unknown_ai_audience_and_changed_cohort(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    assert (await preview(client, "invalid")).status_code == 403
    app.state.assistant.provider = Provider(["group:999999"])
    assert (await client.post("/d/documents/1/notify/suggest", data={"csrf": csrf})).status_code == 400
    assert (await preview(client, csrf, ["group:999999"])).status_code == 400
    assert (await preview(client, csrf, ["all", "group:20"])).status_code == 400
    await preview(client, csrf)
    async with app.state.session_factory() as db:
        await db.execute(update(Student).where(Student.id == 1).values(study_group_id=20))
        await db.commit()
    response = await confirm(client, csrf, app)
    assert response.status_code == 400 and "Состав адресатов изменился" in response.text
    async with app.state.session_factory() as db:
        assert not (await db.scalars(select(DocumentRecipient))).all()


async def test_provider_failure_does_not_block_manual_selection(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    app.state.assistant.provider = Provider(error=True)
    response = await client.post("/d/documents/1/notify/suggest", data={"csrf": csrf})
    assert response.status_code == 400 and "вручную" in response.text
    assert (await preview(client, csrf)).status_code == 200
    assert (await confirm(client, csrf, app)).status_code == 303


class Bot:
    def __init__(self, fail=False):
        self.messages = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="temporary")
        self.messages.append((chat_id, text))


async def with_telegram(app):
    async with app.state.session_factory() as db:
        db.add(TelegramLink(student_id=2, telegram_id=123456789, feed_token="synthetic-feed-token", linked_at=utcnow()))
        await db.commit()


async def test_telegram_success_is_not_repeated(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    await with_telegram(app)
    await preview(client, csrf)
    await confirm(client, csrf, app)
    bot = Bot()
    await deliver_document_notifications(bot, app.state.session_factory)
    await deliver_document_notifications(bot, app.state.session_factory)
    assert len(bot.messages) == 1 and bot.messages[0][0] == 123456789
    assert "Новый документ" in bot.messages[0][1]
    async with app.state.session_factory() as db:
        row = await db.get(DocumentRecipient, (1, 2))
        assert row.telegram_state == "sent" and row.telegram_sent_at


async def test_telegram_failure_retries_and_unlink_skips_delivery(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    await with_telegram(app)
    await preview(client, csrf)
    await confirm(client, csrf, app)
    now = utcnow()
    bot = Bot(fail=True)
    await deliver_document_notifications(bot, app.state.session_factory, now=now)
    async with app.state.session_factory() as db:
        row = await db.get(DocumentRecipient, (1, 2))
        assert row.telegram_state == "pending" and row.telegram_attempts == 1
    bot.fail = False
    await deliver_document_notifications(bot, app.state.session_factory, now=now + timedelta(seconds=61))
    assert len(bot.messages) == 1


async def test_unlinked_telegram_not_sent_or_backfilled(client, tmp_path):
    csrf, app = await prepared(client, tmp_path)
    await with_telegram(app)
    await preview(client, csrf)
    await confirm(client, csrf, app)
    async with app.state.session_factory() as db:
        link = await db.scalar(select(TelegramLink))
        await db.delete(link)
        await db.commit()
    bot = Bot()
    await deliver_document_notifications(bot, app.state.session_factory)
    assert not bot.messages
    async with app.state.session_factory() as db:
        assert (await db.get(DocumentRecipient, (1, 2))).telegram_state == "skipped"
