from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.certificates import service
from app.certificates.pyrus_client import PyrusClient, verify_signature
from app.certificates.service import CertificateSync, UploadedFile
from app.certificates.status import derive
from app.certificates.webhook import router
from app.db.base import Base
from app.db.session import make_engine, make_session_factory
from app.models import CertificateRequest, CertificateRequestEvent, Student
from app.models.certificates import CertificateKind, EventKind, PaperStatus, RequestStatus
from tests.fake_pyrus import AUTH_URL, FakePyrus


@pytest.fixture
async def sf(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    async with factory() as s:
        s.add_all([
            Student(id=1, full_name="Тестова Анна Вымышленовна", gender="f", record_book="Т-0001",
                    faculty_name="Факультет тестовых отношений", course=2, education_basis="договорная",
                    enrollment_order="№ 1-тест от 20.08.2025", enrollment_date=date(2025, 9, 1),
                    expected_graduation=date(2029, 6, 30)),
            Student(id=2, full_name="Примеров Борис Выдуманович", record_book="Т-0002",
                    faculty_name="Факультет примеров", course=1),
        ])
        await s.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def pyrus():
    return FakePyrus()


@pytest.fixture
async def sync(sf, pyrus):
    async with PyrusClient("bot@test", "secret", auth_url=AUTH_URL, transport=pyrus.transport()) as client:
        yield CertificateSync(sf, client, form_id=321)


async def new_request(sf, student_id: int = 1, **kw) -> int:
    async with sf() as s:
        r = await service.create_request(s, student_id, kind=kw.pop("kind", CertificateKind.STUDY), **kw)
        await s.commit()
        return r.id


async def load(sf, request_id: int) -> CertificateRequest:
    async with sf() as s:
        return await s.get(CertificateRequest, request_id)


async def events(sf, request_id: int) -> list[CertificateRequestEvent]:
    async with sf() as s:
        return list((await s.scalars(
            select(CertificateRequestEvent).where(CertificateRequestEvent.request_id == request_id)
            .order_by(CertificateRequestEvent.id)
        )).all())


async def test_create_and_push_to_pyrus(sf, sync, pyrus):
    rid = await new_request(sf, purpose="по месту работы родителей", copies=2)
    assert (await load(sf, rid)).status == RequestStatus.ACCEPTED

    report = await sync.run()
    req = await load(sf, rid)
    assert report.pushed == 1
    assert req.status == RequestStatus.IN_REVIEW
    task = pyrus.tasks[req.pyrus_task_id]
    assert pyrus.field(task["id"], "Студент") == "Тестова Анна Вымышленовна"
    assert pyrus.field(task["id"], "Номер заявки ЛК") == str(rid)
    assert pyrus.field(task["id"], "Количество экземпляров") == "2"
    assert [e.status for e in await events(sf, rid) if e.kind == EventKind.STATUS] == [RequestStatus.IN_REVIEW]


async def test_pyrus_down_keeps_queue_and_retries(sf, sync, pyrus):
    rid = await new_request(sf)
    pyrus.down = True
    report = await sync.run()
    req = await load(sf, rid)
    assert report.push_failed == 1
    assert req.status == RequestStatus.ACCEPTED and req.pyrus_task_id is None
    assert req.last_error and req.push_attempts == 1

    pyrus.down = False
    await sync.run()
    req = await load(sf, rid)
    assert req.status == RequestStatus.IN_REVIEW and req.pyrus_task_id in pyrus.tasks
    assert req.last_error is None


async def test_need_info_reply_with_file_and_no_rollback(sf, sync, pyrus):
    rid = await new_request(sf)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id

    pyrus.set_status(task_id, "Нужна информация")
    pyrus.set_message(task_id, "Уточните, для какой организации справка")
    await sync.run()
    req = await load(sf, rid)
    assert req.status == RequestStatus.NEED_INFO
    assert req.dean_message == "Уточните, для какой организации справка"

    async with sf() as s:
        reply = await service.add_reply(
            s, 1, rid, text="Для банка", files=[UploadedFile("скан.pdf", b"%PDF-fake", "application/pdf")]
        )
        await s.commit()
    assert reply is not None
    assert (await load(sf, rid)).status == RequestStatus.IN_REVIEW

    await sync.run()
    comment = pyrus.tasks[task_id]["comments"][-1]
    assert "Для банка" in comment["text"]
    assert comment["attachments"][0]["name"] == "скан.pdf"
    assert list(pyrus.files.values())[0][1] == b"%PDF-fake"
    # Сотрудник ещё не переключил поле — статус в ЛК не откатывается.
    assert (await load(sf, rid)).status == RequestStatus.IN_REVIEW

    pyrus.set_message(task_id, "Приложите, пожалуйста, ещё копию паспорта")
    await sync.run()
    req = await load(sf, rid)
    assert req.status == RequestStatus.NEED_INFO
    assert req.dean_message.startswith("Приложите")


async def test_reply_retry_does_not_reupload(sf, sync, pyrus):
    rid = await new_request(sf)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id
    pyrus.set_status(task_id, "Нужна информация")
    pyrus.set_message(task_id, "?")
    await sync.run()
    async with sf() as s:
        await service.add_reply(s, 1, rid, text="ответ", files=[UploadedFile("a.txt", b"a")])
        await s.commit()

    # Файл загрузился, а комментарий упал.
    original = pyrus._comment
    pyrus._comment = lambda task, body: httpx.Response(500, json={"error_code": "server_error"})
    report = await sync.run()
    assert report.replies_failed == 1 and len(pyrus.files) == 1
    pyrus._comment = original
    report = await sync.run()
    assert report.replies_pushed == 1 and len(pyrus.files) == 1


async def test_ready_generates_pdf_and_attaches_to_task(sf, sync, pyrus):
    rid = await new_request(sf, kind=CertificateKind.MILITARY)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id
    pyrus.finish(task_id)
    await sync.run()

    req = await load(sf, rid)
    assert req.status == RequestStatus.READY
    assert req.pdf.startswith(b"%PDF") and req.pdf_pushed
    assert pyrus.tasks[task_id]["comments"][-1]["attachments"][0]["name"] == f"spravka-{rid}.pdf"
    kinds = [e.kind for e in await events(sf, rid)]
    assert EventKind.PDF_READY in kinds

    async with sf() as s:
        assert await service.download_pdf(s, 2, rid) is None
        assert (await service.download_pdf(s, 1, rid)).startswith(b"%PDF")

    # Финальные заявки больше не перечитываются.
    before = len(pyrus.log)
    await sync.run()
    assert len(pyrus.log) == before


async def test_rejected_by_approval(sf, sync, pyrus):
    rid = await new_request(sf)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id
    pyrus.reject(task_id)
    pyrus.set_message(task_id, "Справка такого вида выдаётся только с 3 курса")
    await sync.run()
    req = await load(sf, rid)
    assert req.status == RequestStatus.REJECTED
    assert req.dean_message.startswith("Справка такого вида")
    assert req.pdf is None


async def test_student_sees_only_own_requests(sf):
    rid = await new_request(sf, student_id=1)
    async with sf() as s:
        assert await service.get_request(s, 2, rid) is None
        assert await service.get_events(s, 2, rid) is None
        assert await service.list_requests(s, 2) == []
        assert [r.id for r in await service.list_requests(s, 1)] == [rid]
        assert await service.add_reply(s, 2, rid, text="чужое") is None


async def test_validation(sf):
    async with sf() as s:
        with pytest.raises(service.ValidationError):
            await service.create_request(s, 1, kind="diploma")
        with pytest.raises(service.ValidationError):
            await service.create_request(s, 1, kind=CertificateKind.STUDY, copies=0)


async def test_expired_token_reauthorizes(sf, sync, pyrus):
    rid = await new_request(sf)
    await sync.run()
    assert pyrus.auth_calls == 1
    pyrus.revoke_tokens()
    report = await sync.run()
    assert report.refresh_failed == 0 and report.refreshed == 1
    assert pyrus.auth_calls == 2
    assert (await load(sf, rid)).status == RequestStatus.IN_REVIEW


def test_derive_statuses():
    base = {"fields": [], "approvals": [[{"approval_choice": "waiting"}]], "comments": []}
    assert derive(base).status == RequestStatus.IN_REVIEW
    closed = {**base, "comments": [{"action": "finished"}]}
    assert derive(closed).status == RequestStatus.READY
    reopened = {**base, "comments": [{"action": "finished"}, {"action": "reopened"}]}
    assert derive(reopened).status == RequestStatus.IN_REVIEW
    explicit = {**base, "fields": [{"name": "Статус для студента", "value": {"choice_names": ["Готова"]}}]}
    assert derive(explicit).status == RequestStatus.READY
    as_text = {**base, "fields": [{"name": "Статус для студента", "value": "Отказ"}]}
    assert derive(as_text).status == RequestStatus.REJECTED


def test_signature():
    body = b'{"task_id": 1}'
    sig = hmac.new(b"secret", body, hashlib.sha1).hexdigest()
    assert verify_signature(body, sig, "secret")
    assert verify_signature(body, sig.upper(), "secret")
    assert not verify_signature(body, sig, "other")
    assert not verify_signature(body + b" ", sig, "secret")
    assert not verify_signature(body, None, "secret")
    assert not verify_signature(body, sig, "")


async def test_webhook_endpoint(sf, sync, pyrus):
    rid = await new_request(sf)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id
    pyrus.finish(task_id)

    app = FastAPI()
    app.include_router(router)
    app.state.certificate_sync = sync
    app.state.pyrus_security_key = "secret"

    async def post(payload: dict, key: str = "secret"):
        body = json.dumps(payload).encode()
        sig = hmac.new(key.encode(), body, hashlib.sha1).hexdigest()
        return await client.post("/integrations/pyrus/webhook", content=body,
                                 headers={"X-Pyrus-Sig": sig, "Content-Type": "application/json"})

    # ASGITransport в том же event loop; фоновые задачи доходят до конца до возврата ответа.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://lms.test") as client:
        assert (await post({"task_id": task_id}, key="wrong")).status_code == 403
        assert (await post({"nothing": 1})).status_code == 400
        assert (await post({"task_id": 999999})).status_code == 200  # незнакомая задача — молча пропускаем
        assert (await load(sf, rid)).status == RequestStatus.IN_REVIEW
        resp = await post({"event": "comment", "task_id": task_id, "access_token": "x", "task": {"id": task_id}})
        assert resp.status_code == 200
    req = await load(sf, rid)
    assert req.status == RequestStatus.READY and req.pdf_pushed


async def ready_request(sf, sync, pyrus, **kw) -> tuple[int, int]:
    rid = await new_request(sf, **kw)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id
    pyrus.finish(task_id)
    await sync.run()
    return rid, task_id


async def test_paper_gated_by_download_then_reopens_task(sf, sync, pyrus):
    rid, task_id = await ready_request(sf, sync, pyrus)
    async with sf() as s:
        req = await s.get(CertificateRequest, rid)
        assert service.paper_label(req) == service.PAPER_GATE_TEXT
        with pytest.raises(service.ValidationError, match="после скачивания"):
            await service.request_paper(s, 1, rid)
        assert await service.request_paper(s, 2, rid) is None  # чужая
        assert await service.download_pdf(s, 1, rid) is not None
        await s.commit()
    async with sf() as s:
        req = await s.get(CertificateRequest, rid)
        assert req.downloaded_at is not None
        assert service.paper_label(req) is None  # кнопка «Запросить оригинал»
        await service.request_paper(s, 1, rid)
        await s.commit()
        with pytest.raises(service.ValidationError, match="уже запрошен"):
            await service.request_paper(s, 1, rid)

    # Pyrus лежит — запрос ждёт в очереди.
    pyrus.down = True
    report = await sync.run()
    assert report.paper_failed == 1
    req = await load(sf, rid)
    assert req.paper_pushed_at is None and req.paper_last_error

    pyrus.down = False
    report = await sync.run()
    assert report.paper_pushed == 1
    comment = pyrus.tasks[task_id]["comments"][-1]
    assert comment["text"] == service.PAPER_REQUEST_TEXT
    assert comment["action"] == "reopened"  # задача была закрыта

    # Задача переоткрыта и поле пустое: электронная «Готова» не откатывается, бумага — «запрошен».
    await sync.run()
    req = await load(sf, rid)
    assert req.status == RequestStatus.READY
    assert req.paper_status == PaperStatus.REQUESTED
    assert sum(1 for c in pyrus.tasks[task_id]["comments"] if c.get("text") == service.PAPER_REQUEST_TEXT) == 1

    pyrus.set_paper(task_id, "Готов к выдаче")
    await sync.run()
    assert (await load(sf, rid)).paper_status == PaperStatus.READY_FOR_PICKUP
    pyrus.set_paper(task_id, "Выдан")
    await sync.run()
    req = await load(sf, rid)
    assert req.paper_status == PaperStatus.ISSUED
    paper_events = [e.status for e in await events(sf, rid) if e.kind == EventKind.PAPER]
    assert paper_events == [PaperStatus.REQUESTED, PaperStatus.READY_FOR_PICKUP, PaperStatus.ISSUED]

    # Всё выдано — задача больше не перечитывается.
    before = len(pyrus.log)
    await sync.run()
    assert len(pyrus.log) == before


async def test_paper_not_reopened_when_task_open(sf, sync, pyrus):
    rid = await new_request(sf)
    await sync.run()
    task_id = (await load(sf, rid)).pyrus_task_id
    pyrus.set_status(task_id, "Готова")  # готово через поле, задача не закрыта
    await sync.run()
    async with sf() as s:
        await service.download_pdf(s, 1, rid)
        await service.request_paper(s, 1, rid)
        await s.commit()
    await sync.run()
    assert "action" not in pyrus.tasks[task_id]["comments"][-1]


async def test_download_expires(sf, sync, pyrus):
    rid, _ = await ready_request(sf, sync, pyrus)
    req = await load(sf, rid)
    assert req.download_until - req.ready_at == timedelta(days=30)
    later = req.download_until + timedelta(minutes=1)
    assert service.electronic_label(req) == "Готова в электронной форме"
    assert service.electronic_label(req, later) == service.DOWNLOAD_EXPIRED_TEXT
    async with sf() as s:
        assert await service.download_pdf(s, 1, rid, now=later) is None
        assert await service.download_pdf(s, 1, rid, now=req.download_until) is not None


async def test_paper_only_kind(sf, sync, pyrus):
    rid = await new_request(sf, kind=CertificateKind.TRANSFER, purpose="Вымышленный университет")
    req = await load(sf, rid)
    assert req.paper_status == PaperStatus.REQUESTED

    await sync.run()
    req = await load(sf, rid)
    task_id = req.pyrus_task_id
    assert pyrus.field(task_id, "Выдача") == "только на бумаге"
    assert req.paper_pushed_at is not None  # отдельного комментария не будет
    assert not any(c.get("text") == service.PAPER_REQUEST_TEXT for c in pyrus.tasks[task_id]["comments"])

    pyrus.finish(task_id)
    await sync.run()
    req = await load(sf, rid)
    assert req.status == RequestStatus.READY and req.pdf is None
    assert service.electronic_label(req) == "Выдаётся только на бумаге"
    async with sf() as s:
        assert await service.download_pdf(s, 1, rid) is None

    pyrus.set_paper(task_id, "Не востребован")
    await sync.run()
    assert (await load(sf, rid)).paper_status == PaperStatus.NOT_COLLECTED
