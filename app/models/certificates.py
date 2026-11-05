"""Контингент студентов и заявки на справки (решение docs/decisions/0003-dean-office-pyrus.md).

`students` играет роль 1С: в рабочей версии эти поля приходили бы синхронизацией из
1С:Университет, здесь — из seed с вымышленными данными. Реальных ФИО и номеров зачёток
здесь быть не должно.

Источник правды по статусу заявки — задача в Pyrus. В `certificate_requests.status` лежит
последний статус, который мы из неё вывели (см. app/certificates/status.py).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CertificateKind:
    STUDY = "study"
    MILITARY = "military"
    VISA = "visa"
    TRANSFER = "transfer"

    TITLES = {
        STUDY: "Справка об обучении",
        MILITARY: "Справка для военного комиссариата",
        VISA: "Справка для посольства (визовая)",
        TRANSFER: "Справка о периоде обучения (для перевода)",
    }
    PAPER_ONLY = frozenset({TRANSFER})
    """Выдаются только на бумаге (как в РАНХиГС: бланк строгой отчётности). Электронного
    PDF нет, бумажный оригинал запрошен сразу при создании заявки."""


class RequestStatus:
    ACCEPTED = "accepted"
    """Заявка сохранена у нас, задачи в Pyrus ещё нет (очередь отправки)."""
    IN_REVIEW = "in_review"
    NEED_INFO = "need_info"
    READY = "ready"
    REJECTED = "rejected"

    FINAL = (READY, REJECTED)
    TITLES = {
        ACCEPTED: "Принята",
        IN_REVIEW: "На согласовании",
        NEED_INFO: "Нужна информация",
        READY: "Готова",
        REJECTED: "Отказ",
    }


class PaperStatus:
    """Бумажный оригинал — второй, независимый статус заявки (по образцу ЛК РАНХиГС)."""

    NONE = "none"
    REQUESTED = "requested"
    READY_FOR_PICKUP = "ready_for_pickup"
    ISSUED = "issued"
    NOT_COLLECTED = "not_collected"

    ACTIVE = (REQUESTED, READY_FOR_PICKUP)
    """Пока бумага в одном из этих статусов, задачу Pyrus продолжаем перечитывать."""
    TITLES = {
        REQUESTED: "Бумажный оригинал запрошен",
        READY_FOR_PICKUP: "Оригинал готов к выдаче в деканате",
        ISSUED: "Оригинал выдан",
        NOT_COLLECTED: "Оригинал не востребован",
    }


class EventKind:
    CREATED = "created"
    STATUS = "status"
    """Сменился статус; новый — в `status`."""
    DEAN_MESSAGE = "dean_message"
    """Деканат написал студенту (поле «Сообщение студенту»)."""
    STUDENT_REPLY = "student_reply"
    PDF_READY = "pdf_ready"
    PAPER = "paper"
    """Сменился бумажный статус; новый — в `status`."""


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200))
    """Вымышленное ФИО."""
    gender: Mapped[str] = mapped_column(String(1), default="m")
    """m / f — для согласования в тексте справки («обучается», «зачислен(а)»)."""
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    record_book: Mapped[str] = mapped_column(String(50), unique=True)
    """Номер зачётной книжки (вымышленный)."""
    study_group_id: Mapped[int | None] = mapped_column(ForeignKey("study_groups.id"), nullable=True)
    faculty_name: Mapped[str] = mapped_column(String(500), default="")
    course: Mapped[int] = mapped_column(Integer, default=1)
    level: Mapped[str] = mapped_column(String(100), default="бакалавриат")
    form_of_education: Mapped[str] = mapped_column(String(100), default="очная")
    education_basis: Mapped[str] = mapped_column(String(100), default="бюджетная")
    """«бюджетная» / «договорная» — как в тексте справки: «на … основе»."""
    speciality: Mapped[str] = mapped_column(String(500), default="")
    enrollment_order: Mapped[str] = mapped_column(String(200), default="")
    """Например «№ 1234-с от 25.08.2025»."""
    enrollment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_graduation: Mapped[date | None] = mapped_column(Date, nullable=True)


class CertificateRequest(Base):
    __tablename__ = "certificate_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(500), default="")
    """Куда требуется справка (свободный текст студента)."""
    copies: Mapped[int] = mapped_column(Integer, default=1)

    status: Mapped[str] = mapped_column(String(20), default=RequestStatus.ACCEPTED)
    pyrus_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, unique=True)
    dean_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Последнее значение поля «Сообщение студенту» в задаче Pyrus."""
    answered_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Сообщение деканата, на которое студент уже ответил. Пока поле в Pyrus не изменилось,
    повторный «Нужна информация» не показываем: деканат ещё не успел прочитать ответ."""

    push_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    download_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    """До какого момента студент может скачать PDF (ready_at + CERT_DOWNLOAD_DAYS)."""
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    """Первое скачивание PDF. Без него бумажный оригинал не запросить."""

    paper_status: Mapped[str] = mapped_column(String(20), default=PaperStatus.NONE)
    paper_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paper_pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    """Запрос бумаги ушёл в Pyrus комментарием (очередь, как у ответов студента)."""
    paper_push_attempts: Mapped[int] = mapped_column(Integer, default=0)
    paper_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pdf_pushed: Mapped[bool] = mapped_column(default=False)
    """PDF приложен к задаче Pyrus (делается best effort, не блокирует выдачу студенту)."""

    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("ix_certificate_requests_status", "status"),)


class CertificateRequestEvent(Base):
    """Таймлайн заявки в ЛК и очередь уведомлений (Telegram читает строки с пустым notified_at)."""

    __tablename__ = "certificate_request_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("certificate_requests.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class StudentReply(Base):
    """Ответ студента на «Нужна информация». Уходит в Pyrus комментарием (очередь: pushed_at)."""

    __tablename__ = "student_replies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("certificate_requests.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    push_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReplyFile(Base):
    __tablename__ = "reply_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reply_id: Mapped[int] = mapped_column(ForeignKey("student_replies.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    data: Mapped[bytes] = mapped_column(LargeBinary)
    pyrus_guid: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """guid после загрузки в Pyrus: при повторе отправки файл заново не грузим."""
