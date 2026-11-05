"""Заявки на справки: то, что видит студент, и синхронизация с Pyrus.

Правило доступа (CLAUDE.md): всё, что вызывается от имени студента, принимает `student_id`
из сессии и фильтрует по нему в самом запросе к БД. Чужая заявка неотличима от
несуществующей — функции возвращают None, роут отвечает 404.

Синхронизация устойчива к недоступности Pyrus: заявка и ответ студента сначала
сохраняются у нас (очередь), отправка повторяется при следующей сверке.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.certificates.pdf import render
from app.certificates.pyrus_client import PyrusClient, PyrusError
from app.certificates.status import derive, is_closed
from app.models.certificates import (
    CertificateKind,
    CertificateRequest,
    CertificateRequestEvent,
    EventKind,
    PaperStatus,
    ReplyFile,
    RequestStatus,
    Student,
    StudentReply,
)

log = logging.getLogger(__name__)

MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_FILES = 5
MAX_COPIES = 5
REPLY_PREFIX = "Ответ студента из личного кабинета:"
PAPER_REQUEST_TEXT = "Студент запросил бумажный оригинал справки (из личного кабинета)."
PAPER_GATE_TEXT = "Будет доступно после скачивания электронной справки"
DOWNLOAD_EXPIRED_TEXT = "Срок скачивания истёк"


class ValidationError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class UploadedFile:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class SyncReport:
    pushed: int = 0
    push_failed: int = 0
    replies_pushed: int = 0
    replies_failed: int = 0
    paper_pushed: int = 0
    paper_failed: int = 0
    refreshed: int = 0
    refresh_failed: int = 0
    changed: list[int] = field(default_factory=list)


def _event(request: CertificateRequest, kind: str, *, status: str | None = None, text: str | None = None) -> CertificateRequestEvent:
    return CertificateRequestEvent(
        request_id=request.id, kind=kind, status=status, text=text, created_at=utcnow()
    )


# --- сторона студента ---


async def create_request(
    session: AsyncSession, student_id: int, *, kind: str, purpose: str = "", copies: int = 1
) -> CertificateRequest:
    if kind not in CertificateKind.TITLES:
        raise ValidationError(f"неизвестный вид справки: {kind}")
    if not 1 <= copies <= MAX_COPIES:
        raise ValidationError(f"экземпляров — от 1 до {MAX_COPIES}")
    purpose = purpose.strip()[:500]
    now = utcnow()
    paper_only = kind in CertificateKind.PAPER_ONLY
    request = CertificateRequest(
        student_id=student_id, kind=kind, purpose=purpose, copies=copies,
        status=RequestStatus.ACCEPTED, created_at=now, updated_at=now,
        # Справку «только на бумаге» отдельно запрашивать не нужно: запрос уходит
        # вместе с задачей (поле «Выдача»).
        paper_status=PaperStatus.REQUESTED if paper_only else PaperStatus.NONE,
        paper_requested_at=now if paper_only else None,
    )
    session.add(request)
    await session.flush()
    session.add(_event(request, EventKind.CREATED, status=RequestStatus.ACCEPTED))
    if paper_only:
        session.add(_event(request, EventKind.PAPER, status=PaperStatus.REQUESTED,
                           text=PaperStatus.TITLES[PaperStatus.REQUESTED]))
    return request


async def list_requests(session: AsyncSession, student_id: int) -> list[CertificateRequest]:
    rows = await session.scalars(
        select(CertificateRequest)
        .where(CertificateRequest.student_id == student_id)
        .order_by(CertificateRequest.created_at.desc(), CertificateRequest.id.desc())
    )
    return list(rows.all())


async def get_request(session: AsyncSession, student_id: int, request_id: int) -> CertificateRequest | None:
    return await session.scalar(
        select(CertificateRequest).where(
            CertificateRequest.id == request_id, CertificateRequest.student_id == student_id
        )
    )


async def get_events(session: AsyncSession, student_id: int, request_id: int) -> list[CertificateRequestEvent] | None:
    if await get_request(session, student_id, request_id) is None:
        return None
    rows = await session.scalars(
        select(CertificateRequestEvent)
        .where(CertificateRequestEvent.request_id == request_id)
        .order_by(CertificateRequestEvent.id)
    )
    return list(rows.all())


def download_expired(request: CertificateRequest, now: datetime | None = None) -> bool:
    return request.download_until is not None and (now or utcnow()) > request.download_until


def electronic_label(request: CertificateRequest, now: datetime | None = None) -> str:
    """Электронный статус для экрана ЛК (с учётом срока скачивания)."""
    if request.status == RequestStatus.READY:
        if request.kind in CertificateKind.PAPER_ONLY:
            return "Выдаётся только на бумаге"
        if download_expired(request, now):
            return DOWNLOAD_EXPIRED_TEXT
        return "Готова в электронной форме"
    return RequestStatus.TITLES[request.status]


def paper_label(request: CertificateRequest) -> str | None:
    """Бумажный статус для экрана ЛК. None — показать кнопку «Запросить бумажный оригинал»."""
    if request.paper_status != PaperStatus.NONE:
        return PaperStatus.TITLES[request.paper_status]
    if request.status == RequestStatus.REJECTED:
        return "—"
    if request.status != RequestStatus.READY or request.downloaded_at is None:
        return PAPER_GATE_TEXT
    return None


async def download_pdf(
    session: AsyncSession, student_id: int, request_id: int, *, now: datetime | None = None
) -> bytes | None:
    """PDF для студента. Первое скачивание запоминается: оно открывает запрос бумаги.
    None — нет заявки, она не готова, выдаётся только на бумаге или срок скачивания истёк."""
    request = await get_request(session, student_id, request_id)
    if request is None or request.status != RequestStatus.READY or request.pdf is None:
        return None
    if download_expired(request, now):
        return None
    if request.downloaded_at is None:
        request.downloaded_at = now or utcnow()
    return request.pdf


async def request_paper(
    session: AsyncSession, student_id: int, request_id: int, *, now: datetime | None = None
) -> CertificateRequest | None:
    """Запросить бумажный оригинал. None — заявки нет; ValidationError — пока нельзя."""
    request = await get_request(session, student_id, request_id)
    if request is None:
        return None
    if request.status != RequestStatus.READY:
        raise ValidationError("справка ещё не готова")
    if request.paper_status != PaperStatus.NONE:
        raise ValidationError("бумажный оригинал уже запрошен")
    if request.downloaded_at is None:
        raise ValidationError(PAPER_GATE_TEXT)
    now = now or utcnow()
    request.paper_status = PaperStatus.REQUESTED
    request.paper_requested_at = now
    request.paper_pushed_at = None
    request.updated_at = now
    session.add(_event(request, EventKind.PAPER, status=PaperStatus.REQUESTED,
                       text=PaperStatus.TITLES[PaperStatus.REQUESTED]))
    return request


async def add_reply(
    session: AsyncSession, student_id: int, request_id: int, *, text: str, files: list[UploadedFile] = ()
) -> StudentReply | None:
    """Ответ на «Нужна информация». None — заявки нет или она не ждёт ответа."""
    request = await get_request(session, student_id, request_id)
    if request is None or request.status != RequestStatus.NEED_INFO:
        return None
    text = text.strip()
    if not text and not files:
        raise ValidationError("пустой ответ")
    if len(files) > MAX_FILES:
        raise ValidationError(f"не больше {MAX_FILES} файлов")
    for f in files:
        if not f.content:
            raise ValidationError(f"файл {f.filename} пустой")
        if len(f.content) > MAX_FILE_SIZE:
            raise ValidationError(f"файл {f.filename} больше 10 МБ")

    now = utcnow()
    reply = StudentReply(request_id=request.id, text=text, created_at=now)
    session.add(reply)
    await session.flush()
    for f in files:
        session.add(ReplyFile(
            reply_id=reply.id, filename=f.filename[:255] or "file",
            content_type=f.content_type or "application/octet-stream", data=f.content,
        ))
    # Ответ дан на текущее сообщение деканата: пока оно не изменится, не показываем
    # «Нужна информация» снова, даже если сотрудник ещё не переключил поле статуса.
    request.answered_message = request.dean_message
    request.status = RequestStatus.IN_REVIEW
    request.updated_at = now
    session.add(_event(request, EventKind.STUDENT_REPLY, text=text or None))
    session.add(_event(request, EventKind.STATUS, status=RequestStatus.IN_REVIEW))
    return reply


# --- синхронизация с Pyrus ---


def task_fields(student: Student, request: CertificateRequest) -> list[dict[str, Any]]:
    return [
        {"name": "Студент", "value": student.full_name},
        {"name": "Номер зачётки", "value": student.record_book},
        {"name": "Факультет", "value": student.faculty_name},
        {"name": "Курс", "value": student.course},
        {"name": "Форма обучения", "value": student.form_of_education},
        {"name": "Основа обучения", "value": student.education_basis},
        {"name": "Вид справки", "value": CertificateKind.TITLES[request.kind]},
        {"name": "Куда требуется", "value": request.purpose or "по месту требования"},
        {"name": "Количество экземпляров", "value": request.copies},
        {"name": "Номер заявки ЛК", "value": str(request.id)},
        {"name": "Выдача", "value": "только на бумаге" if request.kind in CertificateKind.PAPER_ONLY
                                    else "электронно, оригинал по запросу"},
    ]


class CertificateSync:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: PyrusClient,
        *,
        form_id: int,
        font_path: str = "",
        download_days: int = 30,
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._form_id = form_id
        self._font_path = font_path
        self._download_days = download_days

    async def run(self) -> SyncReport:
        report = SyncReport()
        await self.push_pending_requests(report)
        await self.push_pending_replies(report)
        await self.push_pending_paper(report)
        await self.refresh_open(report)
        return report

    async def push_pending_requests(self, report: SyncReport | None = None) -> None:
        report = report or SyncReport()
        async with self._sf() as s:
            ids = list((await s.scalars(
                select(CertificateRequest.id)
                .where(CertificateRequest.pyrus_task_id.is_(None),
                       CertificateRequest.status == RequestStatus.ACCEPTED)
                .order_by(CertificateRequest.id)
            )).all())
        for request_id in ids:
            async with self._sf() as s:
                request = await s.get(CertificateRequest, request_id)
                student = await s.get(Student, request.student_id)
                request.push_attempts += 1
                try:
                    task = await self._client.create_task(self._form_id, task_fields(student, request))
                except PyrusError as exc:
                    request.last_error = str(exc)[:1000]
                    await s.commit()
                    report.push_failed += 1
                    log.warning("заявка %s не ушла в Pyrus: %s", request_id, exc)
                    continue
                request.pyrus_task_id = int(task["id"])
                request.last_error = None
                if request.paper_status == PaperStatus.REQUESTED:
                    request.paper_pushed_at = utcnow()  # ушло полем «Выдача» вместе с задачей
                await self._apply(s, request, task, report)
                if request.status == RequestStatus.ACCEPTED:
                    self._set_status(s, request, RequestStatus.IN_REVIEW, report)
                await s.commit()
                report.pushed += 1

    async def push_pending_replies(self, report: SyncReport | None = None) -> None:
        report = report or SyncReport()
        async with self._sf() as s:
            ids = list((await s.scalars(
                select(StudentReply.id)
                .join(CertificateRequest, CertificateRequest.id == StudentReply.request_id)
                .where(StudentReply.pushed_at.is_(None), CertificateRequest.pyrus_task_id.is_not(None))
                .order_by(StudentReply.id)
            )).all())
        for reply_id in ids:
            async with self._sf() as s:
                reply = await s.get(StudentReply, reply_id)
                request = await s.get(CertificateRequest, reply.request_id)
                files = list((await s.scalars(
                    select(ReplyFile).where(ReplyFile.reply_id == reply.id).order_by(ReplyFile.id)
                )).all())
                reply.push_attempts += 1
                try:
                    for f in files:
                        if f.pyrus_guid is None:
                            f.pyrus_guid = await self._client.upload_file(f.filename, f.data, f.content_type)
                    text = REPLY_PREFIX + ("\n" + reply.text if reply.text else " (только файлы)")
                    await self._client.add_comment(
                        request.pyrus_task_id, text=text,
                        attachments=[f.pyrus_guid for f in files],
                    )
                except PyrusError as exc:
                    # guid уже загруженных файлов сохраняем, чтобы не грузить повторно.
                    reply.last_error = str(exc)[:1000]
                    await s.commit()
                    report.replies_failed += 1
                    log.warning("ответ %s не ушёл в Pyrus: %s", reply_id, exc)
                    continue
                reply.pushed_at = utcnow()
                reply.last_error = None
                await s.commit()
                report.replies_pushed += 1

    async def push_pending_paper(self, report: SyncReport | None = None) -> None:
        """Запросы бумажного оригинала → комментарий в задачу; закрытую задачу переоткрываем."""
        report = report or SyncReport()
        async with self._sf() as s:
            ids = list((await s.scalars(
                select(CertificateRequest.id)
                .where(CertificateRequest.paper_status == PaperStatus.REQUESTED,
                       CertificateRequest.paper_pushed_at.is_(None),
                       CertificateRequest.pyrus_task_id.is_not(None))
                .order_by(CertificateRequest.id)
            )).all())
        for request_id in ids:
            async with self._sf() as s:
                request = await s.get(CertificateRequest, request_id)
                request.paper_push_attempts += 1
                try:
                    task = await self._client.get_task(request.pyrus_task_id)
                    await self._client.add_comment(
                        request.pyrus_task_id, text=PAPER_REQUEST_TEXT,
                        action="reopened" if is_closed(task) else None,
                    )
                except PyrusError as exc:
                    request.paper_last_error = str(exc)[:1000]
                    await s.commit()
                    report.paper_failed += 1
                    log.warning("запрос бумаги по заявке %s не ушёл в Pyrus: %s", request_id, exc)
                    continue
                request.paper_pushed_at = utcnow()
                request.paper_last_error = None
                await s.commit()
                report.paper_pushed += 1

    async def refresh_open(self, report: SyncReport | None = None) -> None:
        report = report or SyncReport()
        async with self._sf() as s:
            task_ids = list((await s.scalars(
                select(CertificateRequest.pyrus_task_id)
                .where(CertificateRequest.pyrus_task_id.is_not(None),
                       (CertificateRequest.status.not_in(RequestStatus.FINAL))
                       | (CertificateRequest.paper_status.in_(PaperStatus.ACTIVE)))
                .order_by(CertificateRequest.id)
            )).all())
        for task_id in task_ids:
            await self.refresh_task(task_id, report)
        await self.push_ready_pdfs(report)

    async def refresh_task(self, task_id: int, report: SyncReport | None = None) -> bool:
        """Перечитать задачу из Pyrus. False — такой задачи у нас нет (чужая форма, мусор)."""
        report = report or SyncReport()
        async with self._sf() as s:
            request = await s.scalar(select(CertificateRequest).where(CertificateRequest.pyrus_task_id == task_id))
            if request is None:
                return False
            if request.status in RequestStatus.FINAL and request.paper_status not in PaperStatus.ACTIVE:
                return True
            try:
                task = await self._client.get_task(task_id)
            except PyrusError as exc:
                report.refresh_failed += 1
                log.warning("задача Pyrus %s не прочиталась: %s", task_id, exc)
                return True
            await self._apply(s, request, task, report)
            await s.commit()
            report.refreshed += 1
        return True

    async def push_ready_pdfs(self, report: SyncReport | None = None) -> None:
        """Приложить готовые PDF к задачам, чтобы деканат распечатал оригинал. Best effort."""
        async with self._sf() as s:
            ids = list((await s.scalars(
                select(CertificateRequest.id).where(
                    CertificateRequest.status == RequestStatus.READY,
                    CertificateRequest.pdf.is_not(None),
                    CertificateRequest.pdf_pushed.is_(False),
                )
            )).all())
        for request_id in ids:
            async with self._sf() as s:
                request = await s.get(CertificateRequest, request_id)
                try:
                    guid = await self._client.upload_file(f"spravka-{request.id}.pdf", request.pdf, "application/pdf")
                    await self._client.add_comment(
                        request.pyrus_task_id,
                        text="Личный кабинет сформировал PDF справки и отправил студенту. "
                             "Студенту сообщено, что оригинал можно забрать в деканате.",
                        attachments=[guid],
                    )
                except PyrusError as exc:
                    log.warning("PDF заявки %s не приложен к задаче: %s", request_id, exc)
                    continue
                request.pdf_pushed = True
                await s.commit()

    # --- внутреннее ---

    async def _apply(self, s: AsyncSession, request: CertificateRequest, task: dict[str, Any], report: SyncReport) -> None:
        state = derive(task)
        now = utcnow()
        request.synced_at = now

        if state.message != request.dean_message:
            request.dean_message = state.message
            request.updated_at = now
            if state.message:
                s.add(_event(request, EventKind.DEAN_MESSAGE, text=state.message))

        self._apply_paper(s, request, state.paper)
        if request.status in RequestStatus.FINAL:
            # Электронный статус финален: после переоткрытия задачи ради бумаги
            # «Готова» не должна откатиться в «На согласовании».
            return

        status = state.status
        if (status == RequestStatus.NEED_INFO
                and request.answered_message is not None
                and state.message == request.answered_message):
            status = RequestStatus.IN_REVIEW
        if status == request.status:
            return

        if status == RequestStatus.READY:
            request.ready_at = now
            self._set_status(s, request, status, report)
            if request.kind in CertificateKind.PAPER_ONLY:
                return  # электронного PDF нет; дальше следим за полем «Бумажный оригинал»
            request.download_until = now + timedelta(days=self._download_days)
            student = await s.get(Student, request.student_id)
            request.pdf = render(student, request, issued=now.date(), font_path=self._font_path)
            request.pdf_pushed = False
            s.add(_event(
                request, EventKind.PDF_READY,
                text=f"Справка готова: PDF можно скачать в личном кабинете до "
                     f"{request.download_until:%d.%m.%Y}. После скачивания можно запросить "
                     f"бумажный оригинал.",
            ))
            return
        self._set_status(s, request, status, report)

    @staticmethod
    def _apply_paper(s: AsyncSession, request: CertificateRequest, paper: str | None) -> None:
        # Пустое поле не трогает локальный статус: «запрошен» держится, пока деканат
        # не выставил вариант. Ведущий источник — поле формы.
        if paper is None or paper == request.paper_status:
            return
        request.paper_status = paper
        request.updated_at = utcnow()
        s.add(_event(request, EventKind.PAPER, status=paper, text=PaperStatus.TITLES[paper]))

    @staticmethod
    def _set_status(s: AsyncSession, request: CertificateRequest, status: str, report: SyncReport) -> None:
        request.status = status
        request.updated_at = utcnow()
        s.add(_event(request, EventKind.STATUS, status=status))
        report.changed.append(request.id)
