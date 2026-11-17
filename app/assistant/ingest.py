"""CLI подготовки публичного корпуса. Веб-загрузка и скачивание URL намеренно отсутствуют.

python -m app.assistant.ingest manifest.json --out /data/regulations
python -m app.assistant.ingest manifest.json --out /data/regulations --embed
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .corpus import Chunk, Corpus, CorpusError
from app.assistant.repositories import load_corpus


def pdf_pages(path: Path, ocr: str) -> list[str]:
    try:
        import pymupdf
    except ImportError:
        raise CorpusError('Установите зависимости: pip install ".[rag]"') from None
    if path.suffix.lower() != ".pdf" or path.stat().st_size > 50_000_000:
        raise CorpusError("Нужен PDF размером до 50 МБ")
    out = []
    with pymupdf.open(path) as doc:
        if doc.is_encrypted or not 1 <= len(doc) <= 250:
            raise CorpusError("PDF зашифрован или превышен лимит 250 страниц")
        for page in doc:
            text = page.get_text(sort=True).strip()
            if len(re.findall(r"[а-яёa-z]", text, re.I)) < 40:
                if not ocr:
                    raise CorpusError(f"На странице {page.number + 1} нет текста: нужен --tesseract и ручная проверка OCR")
                with tempfile.TemporaryDirectory() as folder:
                    image = Path(folder) / "page.png"
                    page.get_pixmap(dpi=150).save(image)
                    try:
                        result = subprocess.run([ocr, str(image), "stdout", "-l", "rus+eng"],
                                                capture_output=True, timeout=90, check=True)
                    except (OSError, subprocess.SubprocessError):
                        raise CorpusError(f"OCR не завершён на странице {page.number + 1}") from None
                    text = result.stdout.decode("utf-8").strip()
            if len(text) < 30:
                raise CorpusError(f"На странице {page.number + 1} недостаточно текста: проверьте оригинал")
            out.append(text)
    return out


def prepare(manifest: Path, ocr: str = "", *, preview: bool = False) -> list[Chunk]:
    root = manifest.resolve().parent
    records = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise CorpusError("Манифест — список из 1–100 документов")
    chunks = []
    for item in records:
        item = dict(item)
        filename = item.pop("file")
        if item.get("reviewed_public") is not True and not preview:
            raise CorpusError("Сначала подтвердите публичность документа и проверьте редакцию")
        path = (root / filename).resolve()
        if not path.is_relative_to(root):
            raise CorpusError("Файл должен находиться рядом с манифестом или во вложенной папке")
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.stat().st_size <= 50_000_000 else ""
        # Контрольный хэш задаётся человеком ПОСЛЕ сверки файла и OCR с оригиналом.
        reviewed_source = item.pop("reviewed_sha256", None)
        reviewed_text = item.pop("reviewed_text_sha256", None)
        if (reviewed_source != digest or not digest) and not preview:
            raise CorpusError("Хэш файла не соответствует проверенному оригиналу")
        pages = pdf_pages(path, ocr)
        text_digest = hashlib.sha256("\n\f\n".join(pages).encode("utf-8")).hexdigest()
        if not preview and reviewed_text != text_digest:
            raise CorpusError("Текст не сверен с PDF. Сначала выполните --preview, проверьте OCR и укажите reviewed_text_sha256")
        if preview:
            print(json.dumps({"file": filename, "source_sha256": digest, "reviewed_text_sha256": text_digest}, ensure_ascii=False))
        for page, text in enumerate(pages, 1):
            # Разбиваем только перед номером пункта; контекст внутри пункта не обрезаем.
            blocks = re.split(r"\n(?=\s*\d+(?:\.\d+)*[.)]\s+[А-ЯЁA-Z])", text)
            # Короткий заголовок присоединяем к следующему пункту, не теряя текст.
            if len(blocks) > 1 and len(blocks[0].strip()) < 30:
                heading = blocks.pop(0)
                blocks[0] = heading + "\n" + blocks[0]
            for n, block in enumerate(blocks, 1):
                block = block.strip()
                if len(block) < 30:
                    raise CorpusError(f"Короткий фрагмент на странице {page}: нужна ручная подготовка JSONL")
                match = re.match(r"\s*(\d+(?:\.\d+)*[.)])", block)
                clause = "п. " + match[1].rstrip(".)") if match else "стр. " + str(page)
                cid = hashlib.sha256(f"{digest}:{page}:{n}".encode()).hexdigest()[:24]
                metadata = {**item, "reviewed_public": False} if preview else item
                chunks.append(Chunk(id=cid, source_sha256=digest, page=page, clause=clause, text=block, **metadata))
    return chunks


async def add_embeddings(chunks: list[Chunk]):
    from app.core.config import get_settings
    from .provider import GigaChat
    cfg = get_settings()
    if cfg.llm_provider != "gigachat" or not cfg.gigachat_auth_key.get_secret_value() or not cfg.llm_embedding_model:
        raise CorpusError("Для --embed настройте GigaChat и LLM_EMBEDDING_MODEL")
    client = GigaChat(cfg.gigachat_auth_key.get_secret_value(), cfg.gigachat_scope,
                      cfg.gigachat_model, cfg.gigachat_ca_bundle)
    try:
        for offset in range(0, len(chunks), 8):
            batch = chunks[offset:offset+8]
            vectors = await client.embed([c.title + "\n" + c.text for c in batch], cfg.llm_embedding_model)
            for c, vector in zip(batch, vectors):
                c.embedding_model, c.embedding = cfg.llm_embedding_model, vector
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tesseract", default="", help="Путь к установленному tesseract с языками rus+eng")
    parser.add_argument("--embed", action="store_true")
    parser.add_argument("--preview", action="store_true", help="Только извлечь текст для ручной сверки: индекс не включается")
    args = parser.parse_args()
    if args.preview and args.embed:
        parser.error("--preview не отправляет текст провайдеру; уберите --embed")
    chunks = prepare(args.manifest, args.tesseract, preview=args.preview)
    if args.preview:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "extracted-review.jsonl").write_text("\n".join(c.model_dump_json() for c in chunks) + "\n", encoding="utf-8")
        print("Текст сохранён в extracted-review.jsonl с reviewed_public=false; рабочий индекс не изменён.")
        return
    if args.embed:
        asyncio.run(add_embeddings(chunks))
    args.out.mkdir(parents=True, exist_ok=True)
    # Сначала проверяем новый снимок целиком, затем атомарно заменяем индекс.
    pending = args.out / "chunks.pending.jsonl"
    pending.write_text("\n".join(c.model_dump_json() for c in chunks) + "\n", encoding="utf-8")
    corpus = load_corpus(pending)
    os.replace(pending, args.out / "chunks.jsonl")
    print(f"Подготовлено фрагментов: {len(chunks)}; SHA-256 снимка: {corpus.snapshot}")


if __name__ == "__main__":
    main()
