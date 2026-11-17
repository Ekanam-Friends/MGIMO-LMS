"""Упаковать сохранённые публичные PDF и их манифест для добавления на сервер."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--server-manifest',type=Path,help='Подтверждённый манифест сервера: передавать только новые хэши')
    args = parser.parse_args()
    baseline = {row['file']:row for row in csv.DictReader((args.audit/'regulations/manifest.csv').open(encoding='utf-8-sig'))}
    known = baseline
    if args.server_manifest:
        with args.server_manifest.open(encoding='utf-8-sig') as stream:
            known = {row['file']:row for row in csv.DictReader(stream)}
    catalogue = json.loads((args.audit/'catalog-all-links.json').read_text(encoding='utf-8-sig'))
    by_file = {Path(row['url']).name:row for row in catalogue if not row['selection_status'].startswith('excluded_')}
    journal = {}
    log = args.audit/'download-journal.jsonl'
    if log.exists():
        for line in log.read_text(encoding='utf-8').splitlines():
            row = json.loads(line)
            journal[row['filename']] = row
    rows, files = [], []
    for path in sorted((args.audit/'regulations').glob('*.pdf')):
        if not re.fullmatch(r'\d{6}\.pdf',path.name) or path.name not in by_file:
            raise ValueError('Файл вне согласованного перечня НПА')
        data = path.read_bytes()
        if not data.startswith(b'%PDF-'):
            raise ValueError('Ожидался PDF')
        digest = hashlib.sha256(data).hexdigest()
        source = by_file[path.name]
        original = baseline.get(path.name)
        if original and original['sha256'] == digest:
            downloaded = original['downloaded']
        elif path.name in journal and journal[path.name]['sha256'] == digest:
            downloaded = journal[path.name]['downloaded_at'][:10]
        else:
            # Для первого сохранённого PDF журнал ещё не вёлся. Дата локального
            # получения — факт файловой системы, а не дата юридического действия.
            downloaded = datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).date().isoformat()
        rows.append(dict(file=path.name,title=source['title'],url=source['url'],sha256=digest,downloaded=downloaded))
        if path.name not in known or known[path.name]['sha256'] != digest:
            files.append(path)
    manifest = io.StringIO()
    writer = csv.DictWriter(manifest,fieldnames=['file','title','url','sha256','downloaded'])
    writer.writeheader()
    writer.writerows(rows)
    payload = manifest.getvalue().encode('utf-8')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(args.out,'w') as archive:
        member = tarfile.TarInfo('incoming-manifest.csv')
        member.size = len(payload)
        archive.addfile(member,io.BytesIO(payload))
        for path in files:
            archive.add(path,arcname=path.name,recursive=False)
    print(json.dumps({'manifest_documents':len(rows),'new_files':len(files),'archive_bytes':args.out.stat().st_size}))


if __name__ == '__main__':
    main()
