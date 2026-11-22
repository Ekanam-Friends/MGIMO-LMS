"""Подготовка контекста: соседние выдержки одного акта без обрезания текста."""
import json

from .corpus import Chunk


def source_payload(row: Chunk) -> dict:
    return {"id": row.id, "title": row.title, "clause": row.clause, "text": row.text,
            "document_id": row.document_id, "document_family": row.document_family,
            "act_kind": row.act_kind, "related_document_ids": row.related_document_ids,
            "verification_status": row.verification_status,
            "document_date": row.document_date.isoformat() if row.document_date else None}


def evidence_context(hits: list[Chunk], rows: list[Chunk], *,
                     budget: int = 32000, neighbor_radius: int = 2) -> list[Chunk]:
    groups: dict[tuple[str, str], list[Chunk]] = {}
    positions = {}
    for row in rows:
        group = groups.setdefault((row.document_id, row.revision), [])
        positions[row.id] = len(group)
        group.append(row)
    result, seen = [], set()
    used = 2  # Скобки списка sources.
    # Сначала контекст наиболее релевантной выдержки. Старые индексы могут
    # хранить основания в предыдущей выдержке того же акта; второстепенные
    # совпадения не должны вытеснить эти условия из бюджета.
    offsets = (0, -1, 1, -2, 2)[:1 + 2 * neighbor_radius]
    for hit in hits:
        group = groups[(hit.document_id, hit.revision)]
        for offset in offsets:
            index = positions[hit.id] + offset
            if not 0 <= index < len(group):
                continue
            row = group[index]
            if row.id in seen:
                continue
            size = len(json.dumps(source_payload(row), ensure_ascii=False)) + (2 if result else 0)
            if used + size > budget:
                continue
            seen.add(row.id)
            result.append(row)
            used += size
    return result
