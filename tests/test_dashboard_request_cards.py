"""Карточки главной: пограничные состояния, собственный адресат и отсутствие загрузки PDF."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, inspect

from app.certificates.dashboard import dashboard_requests, request_card
from app.certificates.service import create_request
from app.models.certificates import CertificateRequest, PaperStatus, RequestStatus
from tests.test_certificates import sf
from tests.test_web import client, login

NOW = datetime(2026, 11, 17, 12)


@pytest.mark.parametrize("status,tone,label", [
    ("accepted", "grey", "Принята"), ("in_review", "orange", "На согласовании"),
    ("need_info", "red", "Нужна информация"), ("rejected", "red", "Отказ"),
    ("ready", "green", "Готова"),
])
def test_compact_status_preserves_approved_dashboard_design(status, tone, label):
    row = CertificateRequest(id=17, kind="study", status=status, paper_status="none", created_at=NOW)
    card = request_card(row, has_pdf=False, now=NOW)
    assert (card.tone, card.label) == (tone, label)
    assert card.highlight == (status == RequestStatus.READY)
    assert card.number == "0000017" and card.href == "/mfc/certificates/17"
    assert card.download_until is None
    with pytest.raises(FrozenInstanceError):
        card.label = "Изменено"


def test_download_expiry_boundary_and_paper_note_priority():
    row = CertificateRequest(id=17, kind="study", status="ready", paper_status="none",
        created_at=NOW, download_until=NOW)
    assert request_card(row, has_pdf=True, now=NOW).download_until == NOW
    expired = request_card(row, has_pdf=True, now=NOW + timedelta(microseconds=1))
    assert (expired.label, expired.tone, expired.highlight, expired.download_until) == (
        "Срок скачивания истёк", "grey", False, None)
    row.paper_status = PaperStatus.READY_FOR_PICKUP
    paper = request_card(row, has_pdf=True, now=NOW)
    assert paper.paper_note == "оригинал готов к выдаче в деканате"
    assert paper.download_until is None


def test_paper_only_request_keeps_paper_delivery_information():
    row = CertificateRequest(id=18, kind="transfer", status="ready", paper_status="requested", created_at=NOW)
    card = request_card(row, has_pdf=False, now=NOW)
    assert card.label == "Готова"  # Короткий бейдж принятого UX; способ получения — строкой ниже.
    assert card.paper_note == "бумажный оригинал запрошен"
    assert card.download_until is None


async def test_only_three_latest_own_cards_are_loaded_without_pdf_payload(sf):
    async with sf() as db:
        ids = []
        for owner in [1, 1, 1, 1, 2]:
            row = await create_request(db, owner, kind="study", purpose="Синтетическая проверка")
            row.created_at = NOW
            row.pdf = b"%PDF-" + b"x" * 100_000
            ids.append(row.id)
        await db.commit()
    deferred = []
    async with sf() as db:
        @event.listens_for(db.sync_session, "loaded_as_persistent")
        def observe(session, instance):
            if isinstance(instance, CertificateRequest):
                deferred.append("pdf" in inspect(instance).unloaded)
        cards = await dashboard_requests(db, 1, now=NOW)
    assert [card.id for card in cards] == ids[3:0:-1]
    assert ids[-1] not in {card.id for card in cards}
    assert deferred == [True, True, True]


async def test_dashboard_keeps_language_feature_and_student_ownership(client):
    from app.web.i18n import translate
    csrf = await login(client)
    async with client._transport.app.state.session_factory() as db:
        row = await create_request(db, 1, kind="study", purpose="Тест своей карточки")
        row.status = RequestStatus.READY
        await db.commit()
        own_id = row.id
    assert (await client.post("/profile/language", data={"csrf": csrf, "language": "en"})).status_code == 303
    home = await client.get("/")
    assert home.status_code == 200
    assert translate("Мои календари", "en") in home.text
    assert translate("Готова", "en") in home.text
    assert f'class="req-row" href="/mfc/certificates/{own_id}"' in home.text
    assert f'class="req-row" href="/mfc/certificates/{client.boris_request_id}"' not in home.text
