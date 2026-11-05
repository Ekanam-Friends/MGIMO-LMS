"""Клиент Pyrus API v4 (https://pyrus.com/en/help/api).

Используем ровно то, что нужно для справок: авторизация, создание задачи по форме,
чтение задачи, комментарий с вложениями, загрузка файла. Токен кэшируется и
переполучается, если Pyrus сообщил, что он недействителен (срок жизни в документации
не указан, сказано только, что он ограничен).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

import httpx

USER_AGENT = "MGIMO-LMS-prototype/0.1 (certificate requests, concept for a contest)"
DEFAULT_API_URL = "https://api.pyrus.com/v4/"
TOKEN_ERRORS = {"invalid_token", "revoked_token", "expired_token", "token_not_specified"}


class PyrusError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def verify_signature(body: bytes, signature: str | None, security_key: str) -> bool:
    """Подпись вебхука бота: X-Pyrus-Sig = hex(HMAC-SHA1(тело, Security Key бота)).

    Регистр hex у Pyrus в примерах разный (Python — нижний, PHP/C# — верхний),
    поэтому сравниваем без учёта регистра.
    """
    if not signature or not security_key:
        return False
    expected = hmac.new(security_key.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class PyrusClient:
    def __init__(
        self,
        login: str,
        security_key: str,
        *,
        auth_url: str = "https://accounts.pyrus.com/api/v4/auth",
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._login = login
        self._security_key = security_key
        self._auth_url = auth_url
        self._http = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": USER_AGENT}, transport=transport
        )
        self._token: str | None = None
        self._api_url = DEFAULT_API_URL
        self._auth_lock = asyncio.Lock()

    async def __aenter__(self) -> PyrusClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- API ---

    async def create_task(self, form_id: int, fields: list[dict[str, Any]]) -> dict[str, Any]:
        data = await self._request("POST", "tasks", json={"form_id": form_id, "fields": fields})
        return data["task"]

    async def get_task(self, task_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"tasks/{task_id}")
        return data["task"]

    async def add_comment(
        self,
        task_id: int,
        *,
        text: str | None = None,
        attachments: list[str] | None = None,
        field_updates: list[dict[str, Any]] | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        """action: "finished" — закрыть задачу, "reopened" — переоткрыть."""
        body: dict[str, Any] = {}
        if action:
            body["action"] = action
        if text:
            body["text"] = text
        if attachments:
            body["attachments"] = [{"guid": guid} for guid in attachments]
        if field_updates:
            body["field_updates"] = field_updates
        data = await self._request("POST", f"tasks/{task_id}/comments", json=body)
        return data["task"]

    async def upload_file(self, filename: str, content: bytes, content_type: str) -> str:
        data = await self._request(
            "POST", "files/upload", files={"file": (filename, content, content_type)}
        )
        return data["guid"]

    # --- транспорт ---

    async def _authorize(self, stale: str | None) -> str:
        async with self._auth_lock:
            # Пока ждали блокировку, другой запрос мог уже обновить токен.
            if self._token is not None and self._token != stale:
                return self._token
            try:
                resp = await self._http.post(
                    self._auth_url, json={"login": self._login, "security_key": self._security_key}
                )
            except httpx.HTTPError as exc:
                raise PyrusError(f"авторизация: {exc!r}") from exc
            data = _json(resp)
            if resp.status_code != 200 or "access_token" not in data:
                raise PyrusError(
                    f"авторизация: HTTP {resp.status_code} {data.get('error_code')}",
                    code=data.get("error_code"),
                    status=resp.status_code,
                )
            self._token = data["access_token"]
            self._api_url = data.get("api_url") or DEFAULT_API_URL
            return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        token = self._token or await self._authorize(None)
        for attempt in (1, 2):
            try:
                resp = await self._http.request(
                    method,
                    self._api_url.rstrip("/") + "/" + path,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise PyrusError(f"{method} {path}: {exc!r}") from exc
            data = _json(resp)
            code = data.get("error_code")
            if attempt == 1 and (resp.status_code == 401 or code in TOKEN_ERRORS):
                token = await self._authorize(token)
                continue
            if resp.status_code >= 400 or code:
                raise PyrusError(
                    f"{method} {path}: HTTP {resp.status_code} {code}: {data.get('error')}",
                    code=code,
                    status=resp.status_code,
                )
            return data
        raise AssertionError("unreachable")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
