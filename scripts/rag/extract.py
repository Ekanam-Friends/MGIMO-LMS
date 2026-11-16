"""Извлечение PDF для сверки: оригиналы не меняет, рабочий индекс не публикует."""
import csv
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import os
import time
import subprocess
import tempfile
from datetime import datetime, timezone

import pymupdf

def extract_document(item, root, out):
    os.environ['OMP_THREAD_LIMIT'] = '1'
    filename = item['file']
    path = (root / filename).resolve()
    assert path.parent == root.resolve() and path.suffix.lower() == '.pdf'
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == item['sha256'], 'Хэш PDF отличается от манифеста'
    target = out / (path.stem + '.json')
    if target.exists():
        previous = json.loads(target.read_text(encoding='utf-8'))
        if previous.get('source_sha256') == digest and previous.get('status') != 'error':
            return {**{k:v for k,v in previous.items() if k != 'pages'}, 'page_count':len(previous['pages']), 'cached':True}
        if previous.get('source_sha256') != digest:
            raise ValueError('Для новой редакции нужен отдельный идентификатор: исходная сверка сохранена')
    record = {'file': filename, 'title': item['title'], 'source_url': item['url'],
              'source_sha256': digest,
              'downloaded_at': item['downloaded'], 'processed_at': datetime.now(timezone.utc).isoformat(),
              'reviewed_public': False, 'pages': [], 'status': 'pending_review'}
    try:
        assert record['source_sha256'] == item['sha256'], 'Хэш PDF отличается от манифеста'
        pages = []
        with pymupdf.open(path) as doc:
            for page in doc:
                text = page.get_text(sort=True).strip()
                ocr = len(re.findall('[а-яёa-z]', text, re.I)) < 40
                if ocr:
                    with tempfile.TemporaryDirectory() as folder:
                        image = Path(folder) / 'page.png'
                        page.get_pixmap(dpi=200).save(image)
                        result = subprocess.run(['tesseract', str(image), 'stdout', '-l', 'rus+eng'],
                                                capture_output=True, timeout=120, check=True)
                        text = result.stdout.decode('utf-8').strip()
                if page.number == 0:
                    page.get_pixmap(dpi=100).save(out / (path.stem + '-first.png'))
                record['pages'].append({'page': page.number+1, 'text': text, 'ocr': ocr,
                                        'short': len(text) < 30})
                pages.append(text)
        record['reviewed_text_sha256'] = hashlib.sha256('\n\f\n'.join(pages).encode()).hexdigest()
        (out / (path.stem + '.txt')).write_text('\n\f\n'.join(pages), encoding='utf-8')
    except Exception as exc:
        record['status'] = 'error'
        record['error'] = str(exc)
    pending = target.with_suffix('.pending.json')
    pending.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(pending, target)
    return {**{k:v for k,v in record.items() if k != 'pages'}, 'page_count':len(record['pages']), 'cached':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/corpus'))
    parser.add_argument('--out', type=Path, default=Path('/review'))
    parser.add_argument('--workers', type=int, choices=[1,2,3], default=2)
    parser.add_argument('--watch', action='store_true', help='Обрабатывать новые записи атомарно обновляемого манифеста до остановки процесса')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    completed = set()
    report = {}
    print('OCR workers:', args.workers, flush=True)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        while True:
            with (args.root / 'manifest.csv').open(encoding='utf-8-sig') as stream:
                items = list(csv.DictReader(stream))
            pending = {pool.submit(extract_document, item, args.root, args.out):item
                       for item in items if (item['file'],item['sha256']) not in completed}
            for future in as_completed(pending):
                item = pending[future]
                row = future.result()
                completed.add((item['file'],item['sha256']))
                report[item['file']] = row
                target = args.out / 'report.json'
                temporary = target.with_suffix('.pending.json')
                temporary.write_text(json.dumps(list(report.values()), ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary,target)
                print(item['file'], 'cached' if row['cached'] else row['status'], row.get('page_count',''), flush=True)
            if not args.watch:
                break
            time.sleep(5)


if __name__ == '__main__':
    main()
