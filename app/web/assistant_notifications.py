"""Ответ по ленте студента в пределах его сессии."""
import re
from datetime import timezone
from zoneinfo import ZoneInfo

from app.assistant.routing import normalize_scopes
from app.models.notifications import Source
from app.notifications import service as notifications


_NOTIFICATIONS = re.compile(
    r"уведомлен\w*|\bувед(?:ы|ах|ов|ами)\b|\bколокольчик\w*"
)
_SENDER = re.compile(r"\bот\s+(?:(?P<teacher>препод\w*)|(?P<dean>деканат\w*)|(?P<university>вуз\w*))")
_FOLLOWUP = re.compile(
    r"(?:а\s+|нет[, ]+это\s+)?от\s+\w+"
    r"(?:\s+(?:там\s+)?(?:что[- ]то\s+есть|что[- ]то|есть))?"
    r"(?:,?\s+а\s+не\s+от\s+\w+)?[?.!]*"
)
_OTHER_REQUEST = re.compile(
    r"\b(?:расписани\w*|оценк\w*|балл\w*|заявк\w*|справк\w*|"
    r"академ\w*|стипенди\w*|прочита(?:й|ть)|удали\w*|отметь|включи|отключи)\b"
)
MOSCOW = ZoneInfo("Europe/Moscow")
NOTIFICATION_CONTEXT = "Последний вопрос был об уведомлениях студента."


def notification_filter(question: str, previous: str) -> str | None:
    """Пустая строка означает всю ленту, None — другая тема."""
    q = question.lower().replace("ё", "е").strip()
    if not _NOTIFICATIONS.search(q):
        if previous != NOTIFICATION_CONTEXT or not _FOLLOWUP.fullmatch(q):
            return None
    elif _OTHER_REQUEST.search(q) or normalize_scopes(q, ["notifications"]) != ["notifications"]:
        return None
    positive = re.split(r"(?:,?\s+а\s+)?\bне\s+от\s+", q, maxsplit=1)[0]
    sender = _SENDER.search(positive)
    if sender:
        return sender.lastgroup
    return "" if not re.search(r"\bот\s+\w+", q) else None


async def notification_answer(db, viewer, source: str, as_of, today) -> dict:
    if as_of and as_of != today:
        return {"status": "clarify", "message": "Уберите дату, чтобы открыть текущую ленту уведомлений."}
    items = await notifications.feed(db, viewer.user, viewer.student, limit=5,
                                     source=source or None, grade_details=False)
    source_label = {
        Source.TEACHER: "от преподавателей", Source.DEAN: "от деканата", Source.UNIVERSITY: "от вуза",
    }.get(source, "")
    facts = []
    for item in items:
        created = item.created_at.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
        facts.append({"id": item.key, "text": f"{created:%d.%m.%Y} · {item.source_title} · {item.title}",
                      "url": "", "label": "Уведомления"})
    if items:
        heading = f"Последние уведомления {source_label}".rstrip()
        message = f"{heading}. Полный список и тексты доступны в ленте."
    else:
        heading = f"уведомлений {source_label}".rstrip()
        message = f"В текущей ленте за последние {notifications.HORIZON.days} дней {heading} нет."
    return {"status": "answer", "message": message, "facts": facts, "section": "notifications"}
