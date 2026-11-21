"""Кэш чтений с двумя бэкендами: Redis (CACHE_URL задан) и словарь в памяти процесса.

Решение docs/decisions/0027-read-cache.md. Главное:

* значения — JSON-строки (не pickle): из кэша нельзя «распаковать» исполняемый объект;
* инвалидация — через «поколения»: ключ данных содержит текущий токен поколения, а запись
  меняет токен (`bump`). Старые записи никто больше не читает, они уходят по TTL или LRU;
* кэш не обязателен для работы: любая ошибка бэкенда логируется, а вызывающий код идёт в БД
  (fail-open). После ошибки Redis не опрашивается `cooldown` секунд, чтобы недоступный
  сервер не добавлял таймаут к каждому запросу.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

log = logging.getLogger(__name__)

PREFIX = "lms:v1:"
"""Общий префикс: в одном Redis можно держать другие данные, а смена формата — новый префикс."""
GENERATION_TTL = 30 * 86400
"""Токен поколения живёт долго; если его вытеснили, создаётся новый — это тоже инвалидация."""

T = TypeVar("T")


class Backend(Protocol):
    async def mget(self, keys: list[str]) -> list[str | None]: ...

    async def set(self, key: str, value: str, ttl: int, *, nx: bool = False) -> bool: ...

    async def aclose(self) -> None: ...


class MemoryBackend:
    """TTL-словарь с вытеснением давно не читанных записей (LRU) сверх `max_items`.

    Живёт внутри одного процесса: импорт расписания в процессе бота этот кэш не видит, поэтому
    без CACHE_URL свежесть данных web после импорта ограничена только TTL (см. решение 0027).
    """

    def __init__(self, max_items: int = 5000, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_items = max(1, max_items)
        self._clock = clock
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def _alive(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires <= self._clock():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self._alive(k) for k in keys]

    async def set(self, key: str, value: str, ttl: int, *, nx: bool = False) -> bool:
        if nx and self._alive(key) is not None:
            return False
        self._data[key] = (self._clock() + ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)
        return True

    async def aclose(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class RedisBackend:
    """redis.asyncio с короткими таймаутами: кэш не должен тормозить страницу дольше, чем БД."""

    def __init__(self, url: str, *, timeout: float = 0.5) -> None:
        import redis.asyncio as redis

        self._redis = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=timeout, socket_connect_timeout=timeout,
            health_check_interval=30,
        )

    async def mget(self, keys: list[str]) -> list[str | None]:
        return list(await self._redis.mget(keys))

    async def set(self, key: str, value: str, ttl: int, *, nx: bool = False) -> bool:
        return bool(await self._redis.set(key, value, ex=ttl, nx=nx))

    async def aclose(self) -> None:
        await self._redis.aclose()


def new_token() -> str:
    return uuid.uuid4().hex[:12]


class Cache:
    """Фасад над бэкендом: JSON, поколения, fail-open и счётчики для замеров."""

    def __init__(self, backend: Backend, *, cooldown: float = 5.0, schedule_ttl: int = 1800,
                 feed_ttl: int = 60, assistant_ttl: int = 3600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.backend = backend
        self.schedule_ttl, self.feed_ttl, self.assistant_ttl = schedule_ttl, feed_ttl, assistant_ttl
        self.cooldown = cooldown
        self._clock = clock
        self._down_until = 0.0
        self.hits = self.misses = self.errors = 0

    @property
    def kind(self) -> str:
        return "redis" if isinstance(self.backend, RedisBackend) else "memory"

    async def _call(self, op: Callable[[], Awaitable[T]], default: T) -> T:
        if self._clock() < self._down_until:
            return default
        try:
            return await op()
        except Exception as exc:  # noqa: BLE001 — кэш не должен ронять страницу ни при какой ошибке
            self.errors += 1
            self._down_until = self._clock() + self.cooldown
            # Только класс ошибки: в тексте исключения redis бывает адрес с паролем.
            log.warning("кэш недоступен (%s), %.0f с работаем напрямую с БД", type(exc).__name__, self.cooldown)
            return default

    async def get_json(self, key: str) -> Any | None:
        values = await self._call(lambda: self.backend.mget([PREFIX + key]), None)
        raw = values[0] if values else None
        if raw is None:
            self.misses += 1
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            self.misses += 1
            return None
        self.hits += 1
        return value

    async def set_json(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        await self._call(lambda: self.backend.set(PREFIX + key, raw, ttl), False)

    async def generations(self, *names: str) -> list[str] | None:
        """Текущие токены поколений. None — кэш недоступен, вызывающий идёт в БД без кэша."""
        keys = [f"{PREFIX}gen:{n}" for n in names]
        values = await self._call(lambda: self.backend.mget(keys), None)
        if values is None:
            return None
        missing = [k for k, v in zip(keys, values) if v is None]
        if missing:
            # nx: если параллельный процесс успел создать токен, берём его, а не перезаписываем.
            for k in missing:
                await self._call(lambda k=k: self.backend.set(k, new_token(), GENERATION_TTL, nx=True), False)
            values = await self._call(lambda: self.backend.mget(keys), None)
            if values is None or any(v is None for v in values):
                return None
        return list(values)

    async def bump(self, *names: str) -> None:
        """Новое поколение: всё, что закэшировано со старым токеном, больше не читается."""
        for n in names:
            await self._call(lambda n=n: self.backend.set(f"{PREFIX}gen:{n}", new_token(), GENERATION_TTL), False)

    async def aclose(self) -> None:
        try:
            await self.backend.aclose()
        except Exception:  # noqa: BLE001
            log.warning("кэш: не удалось закрыть соединение", exc_info=False)


def make_cache(url: str = "", *, max_items: int = 5000, **ttls: int) -> Cache:
    return Cache(RedisBackend(url) if url else MemoryBackend(max_items=max_items), **ttls)


def cache_from_settings(cfg) -> Cache:
    return make_cache(cfg.cache_url, schedule_ttl=cfg.cache_schedule_ttl, feed_ttl=cfg.cache_feed_ttl,
                      assistant_ttl=cfg.cache_assistant_ttl)


_cache: Cache | None = None


def get_cache() -> Cache:
    """Кэш процесса. Без явной настройки берёт CACHE_URL из окружения (CLI импорта, бот)."""
    global _cache
    if _cache is None:
        try:
            from app.core.config import get_settings

            _cache = cache_from_settings(get_settings())
        except Exception:  # noqa: BLE001 — нет DATABASE_URL и т. п.: работаем с кэшем в памяти
            _cache = make_cache()
    return _cache


def set_cache(cache: Cache | None) -> Cache | None:
    """Подменить кэш процесса (точка входа и тесты). Возвращает прежний."""
    global _cache
    previous, _cache = _cache, cache
    return previous


async def cached(key: str, ttl: int, load: Callable[[], Awaitable[T]], *,
                 dump: Callable[[T], Any], restore: Callable[[Any], T]) -> T:
    """Прочитать из кэша или загрузить и положить. Ключ уже содержит поколения."""
    cache = get_cache()
    hit = await cache.get_json(key)
    if hit is not None:
        try:
            return restore(hit)
        except (KeyError, TypeError, ValueError):
            log.warning("кэш: запись %s в старом формате, читаем из БД", key.split(":", 1)[0])
    value = await load()
    await cache.set_json(key, dump(value), ttl)
    return value
