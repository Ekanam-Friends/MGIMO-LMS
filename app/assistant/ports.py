"""Порты сценариев ассистента: реализация хранения и провайдера остаётся снаружи."""
from datetime import date
from typing import Protocol

from .corpus import Corpus
from .conversation import Conversation


class CorpusRepository(Protocol):
    async def load(self) -> Corpus: ...


class PersonalEvidenceReader(Protocol):
    async def collect(self, student_id: int, scopes: list[str], today: date,
                      start: date, end: date) -> list[dict]: ...


class LanguageModel(Protocol):
    async def complete(self, system: str, data: dict, **kwargs) -> str: ...
    async def embed(self, texts: list[str], model: str) -> list[list[float]]: ...
    async def aclose(self) -> None: ...


class ConversationRepository(Protocol):
    def start(self, owner: str, student_id: int | None = None,
              actor_role: str = "student", language: str = "ru") -> str: ...
    def get(self, token: str, owner: str) -> Conversation | None: ...
    def clear(self, token: str, owner: str) -> None: ...
