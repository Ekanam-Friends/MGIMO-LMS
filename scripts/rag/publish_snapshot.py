"""Публикация справочного снимка из результатов extract.py, без выдуманных дат.

python scripts/rag/publish_snapshot.py --review /data/regulations-review --out /data/regulations
Снимок не считается проверенной действующей редакцией или историческим архивом.
"""
import argparse
import asyncio
import bisect
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re

from app.assistant.corpus import Chunk, Corpus
from app.assistant.repositories import load_corpus
from app.assistant.provider import GigaChat, ProviderError
from app.core.config import get_settings


def fragments(pages):
    text, starts = '', []
    for page in pages:
        starts.append(len(text))
        text += page['text'] + '\n\n'
    # Не разрезаем пункт на границе страницы: сначала соединяем текст документа.
    boundaries = [0] + [m.start() for m in re.finditer(r'(?m)^\s*\d+(?:\.\d+)*\.\s+', text)] + [len(text)]
    boundaries = sorted(set(boundaries))
    units, start = [], 0
    for end in boundaries[1:]:
        if len(text[start:end].strip()) >= 40:
            units.append((start, end))
            start = end
    if start < len(text) and units:
        units[-1] = (units[-1][0], len(text))
    for a, b in units:
        # Позиции берутся из исходного общего текста, поэтому диапазон страниц
        # сохраняется и при разбиении длинного пункта с перекрытием.
        while a < b:
            cut = min(a+2500, b)
            if cut < b:
                boundary = text.rfind('\n', a+1200, cut)
                if boundary < 0:
                    boundary = text.rfind(' ', a+1200, cut)
                if boundary > a:
                    cut = boundary
            content = text[a:cut].strip()
            if len(content) >= 30:
                yield content, bisect.bisect_right(starts, a), bisect.bisect_right(starts, cut-1)
            if cut == b:
                break
            a = max(a+1, cut-200)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--lexical-only', action='store_true', help='BM25 без вызовов эмбеддингов (по умолчанию)')
    mode.add_argument('--embed', action='store_true', help='Явно разрешить API-вызовы эмбеддингов; может требовать платный пакет')
    args = parser.parse_args()
    args.lexical_only = not args.embed
    cfg = get_settings()
    if not args.lexical_only and (not cfg.llm_embedding_model or not cfg.gigachat_auth_key.get_secret_value()):
        raise RuntimeError('Настройте ключ и LLM_EMBEDDING_MODEL')
    chunks, report = [], []
    for path in sorted(args.review.glob('*.json')):
        if path.name == 'report.json':
            continue
        row = json.loads(path.read_text())
        original = args.out / row['file']
        if row['status'] == 'error' or hashlib.sha256(original.read_bytes()).hexdigest() != row['source_sha256']:
            report.append({'file':row['file'], 'status':'error', 'reason':'Ошибка OCR или изменился оригинал'})
            continue
        before = len(chunks)
        for n, (text, page, end) in enumerate(fragments(row['pages']), 1):
            chunks.append(Chunk(id=f"{path.stem}-{n}", document_id=path.stem,
                revision='Загруженный снимок ' + row['downloaded_at'], title=row['title'],
                source_url=row['source_url'], source_sha256=row['source_sha256'],
                document_date=None, effective_from=None, checked_at=date.fromisoformat(row['downloaded_at']),
                downloaded_at=date.fromisoformat(row['downloaded_at']), reviewed_public=True,
                verification_status='snapshot', page=max(1,page), page_end=max(page,end),
                clause='фрагмент '+str(n), text=text))
        report.append({'file':row['file'], 'status':'snapshot', 'chunks':len(chunks)-before,
                       'pages':len(row['pages']), 'reason':'Даты действия и полная сверка OCR не подтверждены'})
    if not chunks:
        raise RuntimeError('Нет фрагментов для публикации')
    cache = args.out / 'embedding-cache'
    cache.mkdir(parents=True, exist_ok=True)
    client = None if args.lexical_only else GigaChat(cfg.gigachat_auth_key.get_secret_value(), cfg.gigachat_scope, cfg.gigachat_model, cfg.gigachat_ca_bundle)
    try:
        for n, chunk in enumerate(chunks, 1):
            if args.lexical_only:
                continue
            text = chunk.title + '\n' + chunk.text
            key = hashlib.sha256((cfg.llm_embedding_model+'\0'+text).encode()).hexdigest()
            stored = cache / (key+'.json')
            if stored.exists():
                vector = json.loads(stored.read_text())
            else:
                for attempt in range(5):
                    try:
                        vector = (await client.embed([text], cfg.llm_embedding_model))[0]
                        break
                    except ProviderError as exc:
                        if str(exc) not in {'GigaChat HTTP 429','GigaChat HTTP 503'} or attempt == 4:
                            raise
                        await asyncio.sleep(min(30, 2 ** (attempt+1)))
                stored.write_text(json.dumps(vector))
            chunk.embedding_model, chunk.embedding = cfg.llm_embedding_model, vector
            if n % 20 == 0 or n == len(chunks):
                print(f'Эмбеддинги: {n}/{len(chunks)}; размерность {len(vector)}', flush=True)
    finally:
        if client:
            await client.aclose()
    pending = args.out / 'chunks.pending.jsonl'
    pending.write_text('\n'.join(c.model_dump_json() for c in chunks)+'\n', encoding='utf-8')
    corpus = load_corpus(pending)
    os.replace(pending, args.out / 'chunks.jsonl')
    (args.out / 'index-report.json').write_text(json.dumps({'snapshot':corpus.snapshot,
        'published_at':date.today().isoformat(), 'model':None if args.lexical_only else cfg.llm_embedding_model, 'chunks':len(chunks),
        'verification_status':'snapshot', 'documents':report}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Опубликован справочный снимок:',len(chunks),'фрагментов;',corpus.snapshot)


if __name__ == '__main__':
    asyncio.run(main())
