"""Правило демоверсии: последняя опубликованная редакция в явно связанной группе актов.

Группы и связи задаются при сверке источников, а не совпадением общих слов в заголовке.
Поправка не отменяет весь исходный акт. Неразборчивые даты не используются для
автоматического выбора победившей редакции.
"""
from datetime import date


def current_published(rows, as_of: date):
    documents = {row.document_id: row for row in rows}
    families = {}
    for row in documents.values():
        if row.document_family and row.publication_checked_at and row.act_kind == "base":
            families.setdefault(row.document_family, []).append(row)
    excluded = set()
    for members in families.values():
        dated = [row for row in members if row.document_date and row.document_date <= as_of
                 and (row.effective_from is None or row.effective_from <= as_of)]
        if not dated:
            continue
        latest = max(row.document_date for row in dated)
        # Не выбираем по имени файла или id при совпадающих датах.
        excluded.update(row.document_id for row in dated if row.document_date < latest)
    for row in documents.values():
        if row.document_family and row.publication_checked_at and row.effective_to and as_of >= row.effective_to:
            excluded.add(row.document_id)
        if row.publication_checked_at and row.document_date and row.document_date > as_of:
            excluded.add(row.document_id)
        if (row.act_kind == "repeal" and row.publication_checked_at and row.document_date
                and row.document_date <= as_of and (row.effective_from is None or row.effective_from <= as_of)):
            excluded.update(row.related_document_ids)
    # Поправки к уже заменённому документу не переносятся на новый автоматически.
    for row in documents.values():
        if row.act_kind == "amendment" and row.related_document_ids and all(key in excluded for key in row.related_document_ids):
            excluded.add(row.document_id)
    return [row for row in rows if row.document_id not in excluded]
