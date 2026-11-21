"""Только GigaChat API: TLS, обновление OAuth, ограниченные ответы и безопасные ошибки."""
from __future__ import annotations

import asyncio
import json
import math
import ssl
import time
import uuid
from email.utils import parsedate_to_datetime

import httpx
from .errors import ProviderError

AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://api.giga.chat/v1"


class GigaChat:
    def __init__(self, key: str, scope: str, model: str, ca_bundle: str = "", *, transport=None, translation_model: str = ""):
        self.key, self.scope, self.model = key, scope, model
        self.translation_model = translation_model or model
        ctx = ssl.create_default_context()
        if ca_bundle:
            ctx.load_verify_locations(cafile=ca_bundle)
        self.http = httpx.AsyncClient(verify=ctx, timeout=httpx.Timeout(30, connect=10),
                                     follow_redirects=False, trust_env=False, transport=transport)
        self._token, self._expires = "", 0.
        self._lock = asyncio.Lock()

    async def aclose(self):
        await self.http.aclose()

    async def _json(self, url: str, **kwargs) -> dict:
        try:
            async with self.http.stream("POST", url, **kwargs) as response:
                response.raise_for_status()
                raw = bytearray()
                async for block in response.aiter_bytes():
                    raw.extend(block)
                    if len(raw) > 1_000_000:
                        raise ProviderError("Ответ провайдера превышает лимит")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("object required")
                return data
        except httpx.HTTPStatusError as exc:
            # Не включать тело ответа, заголовки или текст вопроса в исключение/лог.
            status = exc.response.status_code
            try:
                retry_after = max(0, float(exc.response.headers.get("Retry-After", "0")))
                if not math.isfinite(retry_after):
                    retry_after = 0
            except ValueError:
                try:
                    retry_after = max(0, parsedate_to_datetime(exc.response.headers["Retry-After"]).timestamp() - time.time())
                except (ValueError, TypeError, KeyError, OverflowError):
                    retry_after = 0
            raise ProviderError(f"GigaChat HTTP {status}", code=f"http_{status}",
                retryable=status in {429, 500, 502, 503, 504}, retry_after=retry_after) from None
        except httpx.TimeoutException:
            raise ProviderError("Истекло время ожидания GigaChat", code="timeout", retryable=True) from None
        except httpx.HTTPError:
            raise ProviderError("Не удалось соединиться с GigaChat", code="network", retryable=True) from None
        except ValueError:
            raise ProviderError("Не удалось получить корректный ответ GigaChat", code="invalid_json") from None

    async def _bearer(self) -> str:
        async with self._lock:
            if self._token and self._expires > time.time() + 60:
                return self._token
            data = await self._json(AUTH_URL, headers={"Authorization": "Basic " + self.key,
                                    "RqUID": str(uuid.uuid4()), "Accept": "application/json"},
                                    data={"scope": self.scope})
            token, expires = data.get("access_token"), data.get("expires_at")
            if not isinstance(token, str) or not token or type(expires) not in (int, float):
                raise ProviderError("Некорректный OAuth-ответ")
            self._token, self._expires = token, expires / 1000
            if not math.isfinite(self._expires) or self._expires <= time.time() + 60:
                self._token = ""
                raise ProviderError("Истёкший OAuth-токен")
            return token

    async def _call(self, endpoint: str, payload: dict) -> dict:
        refreshed = False
        retried = False
        while True:
            token = ""
            try:
                token = await self._bearer()
                return await self._json(API_URL + endpoint, headers={"Authorization": "Bearer " + token}, json=payload)
            except ProviderError as exc:
                if exc.code == "http_401" and token and not refreshed:
                    async with self._lock:
                        if self._token == token:
                            self._token, self._expires = "", 0
                    refreshed = True
                    continue
                # Длительные Retry-After не сокращаем: возвращаем ошибку в общий дедлайн.
                if exc.retryable and not retried and exc.retry_after <= 3:
                    retried = True
                    await asyncio.sleep(max(0.5, exc.retry_after))
                    continue
                raise

    async def complete(self, system: str, data: dict, *, max_tokens: int = 2200, response_schema: dict | None = None, model: str | None = None) -> str:
        payload = {
            "model": model or self.model, "messages": [{"role": "system", "content": system},
                {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
            "temperature": 0.1, "max_tokens": max_tokens, "stream": False,
        }
        if response_schema is not None:
            payload["response_format"] = {"type":"json_schema", "schema":response_schema, "strict":True}
        result = await self._call("/chat/completions", payload)
        try:
            choice = result["choices"][0]
            text = choice["message"]["content"]
            reason = choice.get("finish_reason")
            if reason != "stop":
                code = "generation_length" if reason == "length" else "generation_refused"
                raise ProviderError("Генерация не завершена или отклонена GigaChat", code=code)
            if not isinstance(text, str) or not text.strip():
                raise ValueError()
            return text
        except (KeyError, TypeError, IndexError, ValueError):
            raise ProviderError("Пустой или некорректный ответ GigaChat", code="empty_response") from None

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        result = await self._call("/embeddings", {"model": model, "input": texts})
        try:
            data = sorted(result["data"], key=lambda row: row["index"])
            if [r["index"] for r in data] != list(range(len(texts))):
                raise ValueError()
            vectors = [r["embedding"] for r in data]
            size = len(vectors[0])
            if not 1 <= size <= 8192 or any(len(v) != size or not any(v) or
                    any(type(x) not in (int, float) or not math.isfinite(x) for x in v) for v in vectors):
                raise ValueError()
            return vectors
        except (KeyError, TypeError, IndexError, ValueError):
            raise ProviderError("Некорректные эмбеддинги GigaChat") from None
