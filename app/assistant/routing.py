"""Уточнения маршрута до чтения личных данных; нормы остаются в корпусе."""
import re

TOPICS = [(r"стипенд|стипух", "стипендии"),
          (r"академ.{0,15}отпуск", "академического отпуска"),
          (r"материальн.{0,10}помощ", "материальной помощи")]


def resolve_reference(question, history):
    if not re.search(r"\b(?:ее|её|его|этой|этого)\b", question, re.I):
        return question
    if any(re.search(pattern, question, re.I) for pattern, _ in TOPICS):
        return question
    for previous in reversed(history):
        for pattern, subject in TOPICS:
            if re.search(pattern, previous, re.I):
                return re.sub(r"\b(?:ее|её|его|этой|этого)\b", subject, question, flags=re.I)
    return None


def normalize_scopes(question, scopes):
    q = question.lower().replace("ё", "е")
    # Общие правила не требуют чтения оценок, даже если маршрутизатор так решил.
    scholarship = bool(re.search(r"стипенд|стипух", q))
    personal = bool(re.search(r"\b(?:я|мне|меня|мои|моих|моим|моя|мой|мое|наши|наших)\b|положен|полагает|имею ли право", q))
    if scholarship and not personal:
        return ["public"]
    if re.search(r"прогул|пропущ|пропуск.{0,15}(?:пар|занят)|не (?:приду|пойду)", q) and not re.search(r"сколько|мои|у меня", q):
        return ["public"]
    if re.search(r"домашн.{0,12}(?:задан|работ)|\bдз\b", q):
        if not personal and re.search(r"правил|порядок|критери|условия|как.{0,15}(?:оценива|сдава)", q):
            return ["public"]
        result = ["homework"]
        if re.search(r"оценк|балл", q):
            result.append("grades")
        return result
    if re.search(r"как зовут|кто (?:у меня )?(?:ведет|преподает)|имя преподавател", q):
        return ["schedule"]
    return list(dict.fromkeys(scopes))
