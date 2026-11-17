"""Сценарий чтения для главной: готовые карточки вместо бизнес-правил в Jinja."""
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.models.certificates import CertificateKind, CertificateRequest, PaperStatus, RequestStatus
from .service import DOWNLOAD_EXPIRED_TEXT, download_expired, paper_label, utcnow


@dataclass(frozen=True)
class RequestCard:
    id: int
    href: str
    title: str
    number: str
    created_at: datetime
    label: str
    tone: Literal["grey", "green", "red", "orange"]
    highlight: bool
    paper_note: str | None
    download_until: datetime | None


def request_card(request: CertificateRequest, *, has_pdf: bool, now: datetime) -> RequestCard:
    """Компактные подписи и цвета соответствуют принятому дизайну главной PR #37."""
    ready = request.status == RequestStatus.READY
    expired = ready and download_expired(request, now)
    tone = "grey" if expired else {
        RequestStatus.READY: "green", RequestStatus.REJECTED: "red",
        RequestStatus.NEED_INFO: "red", RequestStatus.IN_REVIEW: "orange",
    }.get(request.status, "grey")
    note = paper_label(request) if request.paper_status != PaperStatus.NONE else None
    return RequestCard(
        id=request.id, href=f"/mfc/certificates/{request.id}",
        title=CertificateKind.TITLES.get(request.kind, request.kind), number=f"{request.id:07d}",
        created_at=request.created_at,
        label=DOWNLOAD_EXPIRED_TEXT if expired else RequestStatus.TITLES.get(request.status, request.status),
        tone=tone, highlight=ready and not expired, paper_note=note.lower() if note else None,
        download_until=request.download_until if ready and has_pdf and not expired and not note else None,
    )


async def dashboard_requests(db: AsyncSession, student_id: int, *, now: datetime | None = None) -> list[RequestCard]:
    # PDF не загружается в память ради проверки его наличия. Ограничение и владелец — в SQL.
    rows = await db.execute(select(CertificateRequest, func.length(CertificateRequest.pdf) > 0)
        .options(defer(CertificateRequest.pdf))
        .where(CertificateRequest.student_id == student_id)
        .order_by(CertificateRequest.created_at.desc(), CertificateRequest.id.desc()).limit(3))
    snapshot_time = now or utcnow()
    return [request_card(row, has_pdf=bool(has_pdf), now=snapshot_time) for row, has_pdf in rows]
