"""Сопоставить наблюдавшиеся ссылки с сохранёнными PDF. Ничего не скачивает и не публикует."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads((args.audit / "catalog-all-links.json").read_text(encoding="utf-8-sig"))
    original = {r["file"]: r for r in csv.DictReader((args.audit / "regulations/manifest.csv").open(encoding="utf-8-sig"))}
    journal = {}
    log = args.audit / "download-journal.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            journal[row["url"]] = row
    selected = {}
    excluded = {}
    for row in rows:
        status = row["selection_status"]
        if status.startswith("excluded_"):
            excluded[status] = excluded.get(status, 0) + 1
            continue
        if status == "needs_classification":
            status = "included_normative_attachment" if row["url"].endswith("/004029.pdf") else "included_federal_requirement"
        url = urlsplit(row["url"])
        if url.scheme != "https" or url.hostname != "mgimo.ru" or url.username or url.password:
            raise ValueError("Непубличная ссылка")
        item = selected.setdefault(row["url"], {"title":row["title"],"url":row["url"],"selection_status":status,"source_pages":[]})
        if row["source_page"] not in item["source_pages"]:
            item["source_pages"].append(row["source_page"])
    for item in selected.values():
        name = Path(urlsplit(item["url"]).path).name
        path = args.audit / "regulations" / name
        item.update(filename=name, acquisition_status="missing", ocr_acceptance="pending", legal_currentness="unconfirmed")
        if path.exists():
            data = path.read_bytes()
            if not data.startswith(b"%PDF-"):
                item["acquisition_status"] = "invalid_not_pdf"
                continue
            digest = hashlib.sha256(data).hexdigest()
            item.update(acquisition_status="local_pdf_saved",sha256=digest,bytes=len(data))
            baseline = original.get(name)
            if baseline and baseline["sha256"] == digest:
                item["downloaded_at"] = baseline["downloaded"]
                item["acquisition_origin"] = "baseline_server_corpus"
            elif item["url"] in journal and journal[item["url"]]["sha256"] == digest:
                item["downloaded_at"] = journal[item["url"]]["downloaded_at"]
                item["acquisition_origin"] = "browser_pdf"
            else:
                item["acquisition_origin"] = "local_pdf_requires_provenance_check"
    result = {"inventory_generated_at":datetime.now(timezone.utc).isoformat(),
        "scope":"Публичные НПА раздела Сведения об образовательной организации, нормативные приложения и формы",
        "candidate_documents":len(selected),"local_pdf_saved":sum(r["acquisition_status"] == "local_pdf_saved" for r in selected.values()),
        "excluded_link_occurrences":excluded,
        "note":"Сохранение PDF не означает включение в рабочий индекс, приёмку OCR или подтверждение действия редакции.",
        "documents":sorted(selected.values(), key=lambda r:r["url"])}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k != "documents"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
