"""Ответ по ленте студента в пределах его сессии."""
import re
from dataclasses import dataclass
from datetime import timezone
from zoneinfo import ZoneInfo

from app.assistant.routing import normalize_scopes
from app.models.notifications import Source
from app.notifications import service as notifications


_NOTIFICATIONS = re.compile(
    r"уведомлен\w*|\bувед(?:ы|ах|ов|ами)\b|\bколокольчик\w*"
)
_SENDER = re.compile(r"(?:(?P<teacher>препод\w*)|(?P<dean>деканат\w*)|(?P<university>вуз\w*))")
_SENDER_LIST = re.compile(r"\bот\s+(\w+(?:\s*,?\s+(?:и|или)\s+(?:от\s+)?\w+)*)")
_UNREAD = re.compile(r"\bнепрочитан\w*|\bне\s+прочитан\w*")
_FOLLOWUP = re.compile(
    r"(?:а\s+|нет[, ]+это\s+)?от\s+\w+"
    r"(?:\s*,?\s+(?:и|или)\s+(?:от\s+)?\w+)*"
    r"(?:\s+(?:там\s+)?(?:что[- ]то\s+есть|что[- ]то|есть))?"
    r"(?:,?\s+а\s+не\s+от\s+\w+)?[?.!]*"
)
_OTHER_REQUEST = re.compile(
    r"\b(?:расписани\w*|оценк\w*|балл\w*|заявк\w*|справк\w*|"
    r"академ\w*|стипенди\w*|прочита(?:й|ть)|удали\w*|отметь|включи|отключи)\b"
)
MOSCOW = ZoneInfo("Europe/Moscow")
NOTIFICATION_CONTEXT = "Последний вопрос был об уведомлениях студента."
_SOURCE_LABELS = {Source.TEACHER: "от преподавателей", Source.DEAN: "от деканата", Source.UNIVERSITY: "от вуза"}


@dataclass(frozen=True)
class NotificationFilter:
    sources: tuple[str, ...] | None
    unread_only: bool


def notification_filter(question: str, previous: str) -> NotificationFilter | None:
    q = question.lower().replace("ё", "е").strip()
    if not _NOTIFICATIONS.search(q):
        if previous != NOTIFICATION_CONTEXT or not _FOLLOWUP.fullmatch(q):
            return None
    elif _OTHER_REQUEST.search(q) or normalize_scopes(q, ["notifications"]) != ["notifications"]:
        return None
    positive = re.split(r"(?:,?\s+а\s+)?\bне\s+от\s+", q, maxsplit=1)[0]
    senders = [sender for group in _SENDER_LIST.findall(positive)
               for sender in re.split(r"\s*,?\s+(?:и|или)\s+(?:от\s+)?", group)]
    if not senders and re.search(r"\bот\s+\w+", q):
        return None
    matches = [_SENDER.fullmatch(sender) for sender in senders]
    if any(match is None for match in matches):
        return None
    sources = tuple(dict.fromkeys(match.lastgroup for match in matches if match is not None))
    return NotificationFilter(sources or None, bool(_UNREAD.search(q)))


async def notification_answer(db, viewer, filters: NotificationFilter, as_of, today) -> dict:
    if as_of and as_of != today:
        return {"status": "clarify", "message": "Уберите дату, чтобы открыть текущую ленту уведомлений."}
    items = await notifications.feed(db, viewer.user, viewer.student, limit=5,
                                     sources=filters.sources, grade_details=False, unread_only=filters.unread_only)
    labels = [_SOURCE_LABELS[source] for source in filters.sources or ()]
    source_label = ", ".join(labels[:-1]) + " и " + labels[-1] if len(labels) > 1 else "".join(labels)
    facts = []
    for item in items:
        created = item.created_at.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
        facts.append({"id": item.key, "text": f"{created:%d.%m.%Y} · {item.source_title} · {item.title}",
                      "url": "", "label": "Уведомления"})
    if items:
        title = "Последние непрочитанные уведомления" if filters.unread_only else "Последние уведомления"
        heading = f"{title} {source_label}".rstrip()
        message = f"{heading}. Полный список и тексты доступны в ленте."
    else:
        title = "непрочитанных уведомлений" if filters.unread_only else "уведомлений"
        heading = f"{title} {source_label}".rstrip()
        message = f"В текущей ленте за последние {notifications.HORIZON.days} дней {heading} нет."
    return {"status": "answer", "message": message, "facts": facts, "section": "notifications"}
