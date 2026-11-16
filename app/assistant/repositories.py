"""Файловый адаптер снимка корпуса; сценарий ответа зависит только от порта."""
import asyncio
from pathlib import Path

from .corpus import Corpus, CorpusError


def load_corpus(path: Path) -> Corpus:
    try:
        if path.stat().st_size > 30_000_000:
            raise CorpusError("Индекс слишком большой")
        return Corpus.from_bytes(path.read_bytes())
    except OSError as exc:
        raise CorpusError("Не удалось прочитать снимок корпуса") from exc


class JsonlCorpusRepository:
    def __init__(self, path: Path):
        self.path = path

    async def load(self) -> Corpus:
        return await asyncio.to_thread(load_corpus, self.path)
