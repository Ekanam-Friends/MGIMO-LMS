"""Сборка зависимостей: используется точкой входа, а не доменными сценариями."""
from pathlib import Path

from .conversations import Conversations
from .repositories import JsonlCorpusRepository
from .service import Assistant


def create_assistant(cfg, provider=None) -> Assistant:
    return Assistant(cfg, provider, corpus_repository=JsonlCorpusRepository(
        Path(cfg.rag_corpus_dir) / "chunks.jsonl"), conversations=Conversations())
