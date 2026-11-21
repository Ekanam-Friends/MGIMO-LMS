"""Вход, выход и проверка сессии. Без привязки к FastAPI — тестируется напрямую."""

from __future__ import annotations

import hashlib
import asyncio
import threading
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuthSession, User

_hasher = PasswordHasher()
# Хэш-пустышка: при неизвестном логине всё равно тратим время на проверку,
# чтобы по времени ответа нельзя было перебирать существующие логины.
_DUMMY_HASH = _hasher.hash("timing-equalizer")
_password_slots = threading.BoundedSemaphore(4)


def _check_password(encoded: str, password: str):
    # Лимит удерживает сам поток: отмена HTTP-запроса не освобождает слот,
    # пока argon2 фактически не завершится.
    with _password_slots:
        try:
            _hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False, None
        return True, hash_password(password) if _hasher.check_needs_rehash(encoded) else None


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def authenticate(session: AsyncSession, login: str, password: str, *, release_read_transaction: bool = False) -> User | None:
    user = await session.scalar(select(User).where(User.login == login.strip().lower()))
    encoded = user.password_hash if user else _DUMMY_HASH
    user_id = user.id if user else None
    if release_read_transaction:
        # Только для отдельной сессии /login без ожидающих записи изменений.
        # Не держим соединение БД во время очереди и вычисления argon2.
        await session.commit()
    valid, replacement = await asyncio.to_thread(_check_password, encoded, password)
    if not valid:
        return None
    if user is None:
        return None
    if release_read_transaction:
        # Пароль мог смениться, пока выполнялась проверка в потоке.
        user = await session.scalar(select(User).where(User.id == user_id, User.password_hash == encoded)
                                    .execution_options(populate_existing=True))
        if user is None:
            return None
    if replacement:
        user.password_hash = replacement
    user.last_login_at = utcnow()
    return user


async def open_session(session: AsyncSession, user: User, *, ttl: timedelta) -> str:
    """Создаёт сессию и возвращает токен для cookie (в БД он не хранится)."""
    token = secrets.token_urlsafe(32)
    now = utcnow()
    session.add(AuthSession(
        token_hash=_token_hash(token), user_id=user.id, csrf_token=secrets.token_urlsafe(32),
        created_at=now, expires_at=now + ttl,
    ))
    # Попутно чистим просроченные сессии этого пользователя.
    await session.execute(
        delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at < now)
    )
    return token


async def resolve(session: AsyncSession, token: str | None) -> tuple[User, AuthSession] | None:
    if not token:
        return None
    row = await session.execute(
        select(User, AuthSession)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(AuthSession.token_hash == _token_hash(token), AuthSession.expires_at > utcnow())
    )
    found = row.first()
    return (found[0], found[1]) if found else None


async def close_session(session: AsyncSession, token: str | None) -> None:
    if token:
        await session.execute(delete(AuthSession).where(AuthSession.token_hash == _token_hash(token)))


def csrf_ok(expected: str, given: str | None) -> bool:
    return bool(given) and secrets.compare_digest(expected, given)
