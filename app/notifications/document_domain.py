"""Правила адресации и подтверждения рассылки, без SQL и внешней модели."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import secrets
from typing import Protocol


class NoticeError(ValueError):
    pass


@dataclass(frozen=True)
class AudienceOption:
    key: str
    label: str


def validate_targets(targets: list[str]) -> None:
    if not 1 <= len(targets) <= 20 or len(set(targets)) != len(targets):
        raise NoticeError("Выберите от 1 до 20 разных аудиторий")
    if "all" in targets and len(targets) != 1:
        raise NoticeError("«Все студенты» нельзя совмещать с другими аудиториями")


def fingerprint(ids: list[int]) -> str:
    return hashlib.sha256(",".join(map(str, sorted(ids))).encode()).hexdigest()


@dataclass(frozen=True)
class NoticeDraft:
    confirmed_at: datetime | None
    preview_token: str | None
    preview_by: int | None
    preview_sha256: str | None
    preview_at: datetime | None
    preview_targets: list[str]
    preview_recipients_hash: str | None

    def validate_confirmation(self, actor_id: int, document_sha256: str, token: str, now: datetime) -> None:
        if (not token or not secrets.compare_digest(token, self.preview_token or "")
                or self.preview_by != actor_id or self.preview_sha256 != document_sha256
                or self.preview_at is None or self.preview_at < now - timedelta(minutes=15)):
            raise NoticeError("Предпросмотр устарел. Проверьте получателей заново")


class NoticeRepository(Protocol):
    async def options(self) -> list[AudienceOption]: ...
    async def recipients(self, targets: list[str]) -> list[int]: ...
    async def draft(self, document_id: int) -> NoticeDraft: ...
    async def claim_suggestion(self, document_id: int, now: datetime) -> bool: ...
    async def save_suggestion(self, document_id: int, now: datetime, targets: list[str], explanation: str) -> None: ...
    async def save_preview(self, document_id: int, actor_id: int, document_sha256: str,
                           targets: list[str], ids: list[int], token: str, now: datetime) -> None: ...
    async def confirm(self, document_id: int, actor_id: int, token: str, ids: list[int], now: datetime) -> bool: ...
