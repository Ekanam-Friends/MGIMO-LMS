"""Добавить PDF с проверкой хэшей и атомарно расширить манифест. Индекс не меняет."""
import argparse
import csv
from datetime import date
import hashlib
import io
import os
from pathlib import Path
import re
import tarfile
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    manifest = root/'manifest.csv'
    with manifest.open(encoding='utf-8-sig') as stream:
        existing = {row['file']:row for row in csv.DictReader(stream)}
    added = 0
    with tarfile.open(args.archive,'r') as archive:
        members = archive.getmembers()
        if any(not item.isfile() or (item.name != 'incoming-manifest.csv' and not re.fullmatch(r'\d{6}\.pdf',item.name)) for item in members):
            raise ValueError('Недопустимое содержимое архива')
        rows = list(csv.DictReader(io.StringIO(archive.extractfile('incoming-manifest.csv').read().decode('utf-8-sig'))))
        if len({row['file'] for row in rows}) != len(rows):
            raise ValueError('Дубли манифеста')
        for row in rows:
            name = row['file']
            if not re.fullmatch(r'\d{6}\.pdf',name) or not re.fullmatch(r'[a-f0-9]{64}',row['sha256']):
                raise ValueError('Некорректный манифест')
            url = urlsplit(row['url'])
            if url.scheme != 'https' or url.hostname != 'mgimo.ru' or url.username or url.password:
                raise ValueError('Непубличный источник')
            date.fromisoformat(row['downloaded'])
            target = root/name
            if target.parent != root:
                raise ValueError('Файл вне корпуса')
            if target.exists():
                data = target.read_bytes()
                if hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('На сервере уже есть другая редакция: замена запрещена')
            else:
                data = archive.extractfile(name).read()
                if not data.startswith(b'%PDF-') or hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('Повреждённый PDF')
                pending = target.with_suffix('.pending')
                pending.write_bytes(data)
                os.replace(pending,target)
                added += 1
            if name in existing:
                row['downloaded'] = min(row['downloaded'],existing[name]['downloaded'])
            existing[name] = row
    pending = root/'manifest.pending.csv'
    with pending.open('w',encoding='utf-8',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=['file','title','url','sha256','downloaded'],extrasaction='ignore')
        writer.writeheader()
        writer.writerows(existing[key] for key in sorted(existing))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending,manifest)
    print('Документов в манифесте:',len(existing),'; добавлено PDF:',added,flush=True)


if __name__ == '__main__':
    main()
