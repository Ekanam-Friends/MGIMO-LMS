"""Клиент открытого API РУЗ МГИМО (https://ruz.mgimo.ru/api/).

API недокументирован, эндпоинты найдены по запросам самого сайта — см.
docs/decisions/0002-schedule-source.md. Сервер чужой, поэтому клиент сам выдерживает
паузу между началами запросов; сколько запросов идёт одновременно, решает импортёр
(по замеру 2026-11-05 предел без роста времени ответа — 2 потока, см. решение 0002).
"""

from __future__ import annotations

import asyncio
import time
import logging
from urllib.parse import urlsplit
from datetime import date
from typing import Any

import httpx

USER_AGENT = "MGIMO-LMS-prototype/0.1 (schedule importer, concept for a contest)"


class RuzError(RuntimeError):
    pass


def ruz_date(value: date) -> str:
    return value.strftime("%Y.%m.%d")


class RuzClient:
    def __init__(
        self,
        base_url: str,
        *,
        delay: float = 1.0,
        timeout: float = 120.0,
        retries: int = 3,
        backoff: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        verify_tls: bool = True,
    ) -> None:
        target = urlsplit(base_url)
        if not verify_tls:
            if target.scheme != "https" or target.hostname != "ruz.mgimo.ru" or target.port not in (None, 443) or target.username or target.password:
                raise ValueError("Исключение TLS разрешено только для https://ruz.mgimo.ru:443")
            logging.getLogger(__name__).warning("РУЗ: проверка TLS отключена явной настройкой только для ruz.mgimo.ru")
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            transport=transport,
            verify=verify_tls,
            follow_redirects=False,
        )
        self._delay = delay
        self._retries = retries
        self._backoff = backoff
        self._last_request: float | None = None
        self._throttle_lock = asyncio.Lock()

    async def __aenter__(self) -> RuzClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def faculties(self) -> list[dict[str, Any]]:
        return await self._get_list("dictionary/faculties")

    async def groups(self) -> list[dict[str, Any]]:
        return await self._get_list("dictionary/groups")

    async def lecturers(self) -> list[dict[str, Any]]:
        """Справочник преподавателей. Содержит реальные ФИО — см. normalize.real_surnames."""
        return await self._get_list("dictionary/lecturers")

    async def group_schedule(self, group_id: int, start: date, finish: date) -> list[dict[str, Any]]:
        return await self._get_list(
            f"schedule/group/{group_id}",
            {"start": ruz_date(start), "finish": ruz_date(finish), "lng": 1},
        )

    async def _get_list(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        data = await self._get(path, params)
        if isinstance(data, dict) and data.get("error"):
            raise RuzError(f"{path}: РУЗ вернул ошибку: {data.get('message')!r}")
        if not isinstance(data, list):
            raise RuzError(f"{path}: ожидался список, пришёл {type(data).__name__}")
        return data

    async def _get(self, path: str, params: dict[str, Any] | None) -> Any:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff * 2 ** (attempt - 1))
            await self._throttle()
            try:
                response = await self._http.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuzError(f"{path}: HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise RuzError(f"{path}: HTTP {response.status_code}")
            try:
                return response.json()
            except ValueError as exc:
                raise RuzError(f"{path}: ответ не JSON") from exc
        raise RuzError(f"{path}: не удалось после {self._retries + 1} попыток; причина: {last_error}") from last_error

    async def _throttle(self) -> None:
        """Между началами любых двух запросов — не меньше `delay`, в том числе при параллельной загрузке."""
        async with self._throttle_lock:
            now = time.monotonic()
            if self._last_request is not None:
                wait = self._delay - (now - self._last_request)
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_request = time.monotonic()
