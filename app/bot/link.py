"""Привязка Telegram к студенту: одноразовый код из профиля → `/start <код>` в боте.

Кто есть кто, бот узнаёт только отсюда: `TelegramLink` ищется по telegram id отправителя
апдейта, а не по чему-то, что человек может прислать текстом.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotNotice, Student, TelegramLink, TelegramLinkCode


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def new_feed_token() -> str:
    return secrets.token_urlsafe(24)


async def issue_code(session: AsyncSession, student_id: int, *, ttl: timedelta) -> str:
    """Новый код привязки. Прежние неиспользованные коды студента гасятся."""
    await session.execute(
        delete(TelegramLinkCode).where(TelegramLinkCode.student_id == student_id, TelegramLinkCode.used_at.is_(None))
    )
    # 24 байта → 32 символа [A-Za-z0-9_-]: влезает в лимит deep-link Telegram (64 символа).
    code = secrets.token_urlsafe(24)
    session.add(TelegramLinkCode(code_hash=_hash(code), student_id=student_id, expires_at=utcnow() + ttl))
    await session.flush()
    return code


class LinkError(Exception):
    pass


async def consume_code(session: AsyncSession, code: str, telegram_id: int) -> tuple[TelegramLink, Student]:
    """Привязать Telegram по коду. Код одноразовый; просроченный или чужой — LinkError.

    Если этот Telegram уже был привязан к другому студенту, привязка переезжает: код
    выдан кабинетом после входа, он и есть подтверждение. Если у студента был другой
    Telegram, тот отключается — у студента один чат с ботом.
    """
    row = await session.get(TelegramLinkCode, _hash(code.strip()))
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at < now:
        raise LinkError("Код не подошёл: он одноразовый и живёт 15 минут. Получите новый в профиле кабинета.")
    student = await session.get(Student, row.student_id)
    if student is None:
        raise LinkError("Студент не найден.")
    row.used_at = now

    await session.execute(
        delete(TelegramLink).where(
            (TelegramLink.telegram_id == telegram_id) & (TelegramLink.student_id != student.id)
        )
    )
    link = await session.scalar(select(TelegramLink).where(TelegramLink.student_id == student.id))
    if link is None:
        link = TelegramLink(student_id=student.id, telegram_id=telegram_id, linked_at=now, feed_token=new_feed_token(),
                            digest_at="07:30", notify_changes=True, notify_certificates=True, exam_reminder_days=3)
        session.add(link)
    else:
        link.telegram_id = telegram_id
        link.linked_at = now
    await session.flush()
    return link, student


async def by_telegram(session: AsyncSession, telegram_id: int) -> tuple[TelegramLink, Student] | None:
    link = await session.scalar(select(TelegramLink).where(TelegramLink.telegram_id == telegram_id))
    if link is None:
        return None
    student = await session.get(Student, link.student_id)
    return (link, student) if student is not None else None


async def by_student(session: AsyncSession, student_id: int) -> TelegramLink | None:
    return await session.scalar(select(TelegramLink).where(TelegramLink.student_id == student_id))


async def by_feed_token(session: AsyncSession, token: str) -> tuple[TelegramLink, Student] | None:
    link = await session.scalar(select(TelegramLink).where(TelegramLink.feed_token == token))
    if link is None:
        return None
    student = await session.get(Student, link.student_id)
    return (link, student) if student is not None else None


async def unlink(session: AsyncSession, student_id: int) -> bool:
    """Отключить бота: удаляются привязка (с ней — ссылка на календарь), коды и журнал напоминаний."""
    link = await by_student(session, student_id)
    await session.execute(delete(TelegramLinkCode).where(TelegramLinkCode.student_id == student_id))
    await session.execute(delete(BotNotice).where(BotNotice.student_id == student_id))
    if link is None:
        return False
    await session.delete(link)
    await session.flush()
    return True


def rotate_feed_token(link: TelegramLink) -> str:
    link.feed_token = new_feed_token()
    return link.feed_token
