"""Один движок на процесс, сессия — на единицу работы."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def make_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: после commit объекты читаются без скрытого SELECT,
    # который в async-коде превратился бы в ошибку.
    return async_sessionmaker(engine, expire_on_commit=False)
