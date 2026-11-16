"""Проверенный снимок корпуса, редакции и двухступенчатый гибридный поиск."""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import snowballstemmer
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorpusError(ValueError):
    pass


def public_url(value: str) -> str:
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if (url.scheme != "https" or not (host == "mgimo.ru" or host.endswith(".mgimo.ru"))
            or url.username or url.password or url.port not in (None, 443) or url.fragment
            or any(ord(c) < 33 for c in value) or "\\" in value):
        raise ValueError("Нужна HTTPS-ссылка на официальный публичный сайт МГИМО")
    return value


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,120}$")
    document_id: str = Field(min_length=1, max_length=120)
    revision: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(max_length=2000)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_date: date | None
    effective_from: date | None
    effective_to: date | None = None  # исключающая правая граница
    retroactive_basis: str | None = Field(default=None, max_length=1000)
    checked_at: date
    downloaded_at: date
    reviewed_public: bool = False
    verification_status: Literal["verified", "snapshot"] = "verified"
    document_family: str | None = Field(default=None, max_length=120)
    act_kind: Literal["base", "amendment", "repeal"] = "base"
    related_document_ids: list[str] = Field(default_factory=list, max_length=100)
    publication_checked_at: date | None = None
    publication_source_url: str | None = Field(default=None, max_length=2000)
    page: int = Field(ge=1, le=10000)
    page_end: int | None = Field(default=None, ge=1, le=10000)
    clause: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=30, max_length=6000)
    embedding_model: str = ""
    embedding: list[float] = Field(default_factory=list, max_length=8192)

    @model_validator(mode="after")
    def valid_metadata(self):
        public_url(self.source_url)
        if self.publication_checked_at:
            if not self.publication_source_url:
                raise ValueError("Нужна страница опубликованного акта")
            public_url(self.publication_source_url)
        if self.verification_status == "verified" and (self.document_date is None or self.effective_from is None):
            raise ValueError("Проверенной редакции нужны подтверждённые даты")
        if self.page_end is not None and self.page_end < self.page:
            raise ValueError("Конец фрагмента раньше начала")
        if self.effective_to is not None and self.effective_from is not None and self.effective_to <= self.effective_from:
            raise ValueError("Некорректный интервал действия редакции")
        if self.effective_from and self.document_date and self.effective_from < self.document_date and not self.retroactive_basis:
            raise ValueError("Обратная сила требует проверенного основания в документе")
        if (self.document_date and self.document_date > self.checked_at) or self.downloaded_at > self.checked_at:
            raise ValueError("Проверка источника не может предшествовать документу или скачиванию")
        if self.embedding and (not self.embedding_model or not all(math.isfinite(v) for v in self.embedding)
                               or not any(self.embedding)):
            raise ValueError("Некорректный вектор")
        return self

    @property
    def original_url(self) -> str:
        return f"{self.source_url}#page={self.page}"


_STEMMER = snowballstemmer.stemmer("russian")
_STOP = set("как какой какая какие сколько когда где куда кто что нужно мне мне это для при или чтобы можно ли оформить получить подать документы документ студент студента обучающийся пожалуйста расскажи".split())
_ALIASES = {"академ": "академический отпуск", "общага": "общежитие", "общагу": "общежитие",
            "стипуха": "стипендия", "матпомощь": "материальная помощь", "бюджет": "бесплатное обучение"}


def terms(text: str) -> list[str]:
    words = re.findall(r"[а-яёa-z]{3,}", text.lower().replace("ё", "е"))
    return _STEMMER.stemWords([w for w in words if w not in _STOP])


def expand(question: str) -> str:
    return re.sub(r"\b(" + "|".join(_ALIASES) + r")\b", lambda m: _ALIASES[m[0]], question.lower())


def _bm25(query: list[str], texts: list[str]) -> list[float]:
    bags = [Counter(terms(t)) for t in texts]
    n = len(bags)
    avg = sum(sum(b.values()) for b in bags) / max(n, 1) or 1
    dfs = {t: sum(t in b for b in bags) for t in set(query)}
    return [sum(math.log(1 + (n - dfs[t] + .5) / (dfs[t] + .5)) * b[t] * 2.5 /
                (b[t] + 1.5 * (.25 + .75 * sum(b.values()) / avg))
                for t in set(query) if b[t]) for b in bags]


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a or not all(math.isfinite(x) for x in a + b):
        raise CorpusError("Размерность или значения эмбеддингов не совпадают")
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    if not norm:
        raise CorpusError("Нулевой эмбеддинг")
    return sum(x*y for x, y in zip(a, b)) / norm


class Corpus:
    def __init__(self, chunks: list[Chunk], snapshot: str):
        self.chunks, self.snapshot = chunks, snapshot

    @classmethod
    def load(cls, path: Path) -> "Corpus":
        try:
            if path.stat().st_size > 30_000_000:
                raise CorpusError("Индекс слишком большой")
            raw = path.read_bytes()
            rows = [Chunk.model_validate_json(line) for line in raw.splitlines() if line.strip()]
            if not rows or len(rows) > 15000 or len({r.id for r in rows}) != len(rows):
                raise CorpusError("Пустой индекс, превышен лимит или повторяются ID")
            metadata = {}
            for row in rows:
                key = row.document_id, row.revision
                value = row.model_dump(exclude={"id", "page", "page_end", "clause", "text", "embedding"})
                if key in metadata and metadata[key] != value:
                    raise CorpusError("Противоречивые метаданные редакции")
                metadata[key] = value
            return cls(rows, hashlib.sha256(raw).hexdigest())
        except (OSError, ValueError) as exc:
            raise CorpusError("Не удалось проверить снимок корпуса") from exc

    def active(self, as_of: date, today: date, max_age: int) -> list[Chunk]:
        from .revisions import current_published
        published = current_published([c for c in self.chunks if c.reviewed_public
            and c.checked_at <= today and c.downloaded_at <= today
            and (c.publication_checked_at is None or c.publication_checked_at <= today)], as_of)
        rows = [c for c in published if c.reviewed_public
                and ((c.verification_status == "snapshot" and as_of == today)
                     or (c.verification_status == "verified" and c.effective_from is not None and c.effective_from <= as_of))
                and (c.effective_to is None or as_of < c.effective_to)
                and (c.effective_from is None or c.effective_from <= as_of)
                and c.checked_at <= today and c.downloaded_at <= today
                and (today - c.checked_at).days <= max_age]
        revisions: dict[str, set[str]] = {}
        for c in rows:
            revisions.setdefault(c.document_id, set()).add(c.revision)
        if any(len(v) > 1 for v in revisions.values()):
            raise CorpusError("Пересекаются интервалы действия редакций")
        return rows

    def search(self, question: str, rows: list[Chunk], top_k: int,
               vector: list[float] | None = None, *, lexical_weight: float = 1., semantic_weight: float = 1.) -> list[Chunk]:
        query = terms(expand(question))
        if not query or not rows:
            return []
        by_doc: dict[str, list[Chunk]] = {}
        for c in rows:
            by_doc.setdefault(c.document_family or c.document_id, []).append(c)
        docs = list(by_doc)
        texts = [by_doc[d][0].title + " " + " ".join(c.text for c in by_doc[d]) for d in docs]
        doc_scores = _bm25(query, texts)
        selected = sorted(range(len(docs)), key=lambda i: doc_scores[i], reverse=True)[:2]
        candidates = [c for i in selected if doc_scores[i] > 0 for c in by_doc[docs[i]]]
        scores = _bm25(query, [c.title + " " + c.text for c in candidates])
        # Совпадение общей лексики не доказывает, что в корпусе есть нужная процедура.
        meaningful = set(query)
        relevant = [i for i, c in enumerate(candidates)
                    if len(meaningful & set(terms(c.title + " " + c.text))) / len(meaningful) >= .5]
        lexical = sorted(relevant, key=lambda i: scores[i], reverse=True)
        # Лексический канал сохраняет фильтр совпадения процедуры. Семантический
        # ищет по всему корпусу: иначе перефразирование отсекается ещё до векторов.
        ranks = {candidates[i].id: lexical_weight/(60+r) for r, i in enumerate(lexical, 1)}
        by_id = {c.id: c for c in rows}
        if vector is not None:
            similarities = [(c.id, _cosine(vector, c.embedding)) for c in rows]
            dense = sorted(similarities, key=lambda pair: pair[1], reverse=True)[:max(12, top_k * 4)]
            for r, (cid, _) in enumerate(dense, 1):
                ranks[cid] = ranks.get(cid, 0.) + semantic_weight/(60+r)
        return [by_id[cid] for cid in sorted(ranks, key=ranks.get, reverse=True)[:top_k]]
