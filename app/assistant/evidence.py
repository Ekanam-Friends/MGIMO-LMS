"""Ограничение контекста сохраняет каждую запрошенную область и сообщает о пропусках."""
from collections import deque


def bounded_facts(facts: list[dict], maximum: int = 160) -> list[dict]:
    if len(facts) <= maximum:
        return facts
    groups = {}
    for fact in facts:
        groups.setdefault(fact["label"], deque()).append(fact)
    result = []
    while len(result) < maximum - 1 and any(groups.values()):
        for group in groups.values():
            if group and len(result) < maximum - 1:
                result.append(group.popleft())
    result.append({"id": "fact-limit", "text": "Выборка ограничена; часть записей не включена. Уточните вопрос или откройте нужный раздел кабинета.",
                   "url": "", "label": "Ограничение выборки"})
    return result
