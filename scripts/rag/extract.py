"""Извлечение PDF для сверки: оригиналы не меняет, рабочий индекс не публикует."""
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone

import pymupdf

root = Path('/corpus')
out = Path('/review')
out.mkdir(parents=True, exist_ok=True)
report = []
for item in csv.DictReader((root / 'manifest.csv').open(encoding='utf-8-sig')):
    filename = item['file']
    path = (root / filename).resolve()
    assert path.parent == root.resolve() and path.suffix.lower() == '.pdf'
    record = {'file': filename, 'title': item['title'], 'source_url': item['url'],
              'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
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
    (out / (path.stem + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    report.append({k:v for k,v in record.items() if k != 'pages'})
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(filename, record['status'], len(record['pages']), flush=True)
