"""Как задача Pyrus превращается в статус заявки в ЛК.

Главный источник — поле формы «Статус для студента» (список), его выставляет сотрудник
деканата. Сообщение студенту — отдельное текстовое поле «Сообщение студенту»: так
внутренняя переписка в комментариях задачи студенту не видна.

Если поле не заполнено, статус выводится из самой задачи: отклонение на любом шаге →
«Отказ», задача закрыта → «Готова», иначе — «На согласовании».
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.certificates import PaperStatus, RequestStatus

STATUS_FIELD = "Статус для студента"
MESSAGE_FIELD = "Сообщение студенту"
PAPER_FIELD = "Бумажный оригинал"

CHOICE_TO_STATUS = {
    "на согласовании": RequestStatus.IN_REVIEW,
    "нужна информация": RequestStatus.NEED_INFO,
    "готова": RequestStatus.READY,
    "отказ": RequestStatus.REJECTED,
}

# Поле выставляет только деканат. Через API мы его не пишем: запрос студента уходит
# комментарием, а локальный «запрошен» держится, пока поле пустое.
CHOICE_TO_PAPER = {
    "готов к выдаче": PaperStatus.READY_FOR_PICKUP,
    "выдан": PaperStatus.ISSUED,
    "не востребован": PaperStatus.NOT_COLLECTED,
}


@dataclass(frozen=True)
class TaskState:
    status: str
    message: str | None
    paper: str | None = None
    """Бумажный статус из поля формы; None — поле пустое."""


def field_value(task: dict[str, Any], name: str) -> Any:
    for field in task.get("fields") or []:
        if field.get("name") == name or field.get("code") == name:
            return field.get("value")
    return None


def _choice_name(value: Any) -> str | None:
    """Значение поля-списка. В ответах Pyrus — {"choice_ids": [...], "choice_names": [...]};
    текстовое поле с тем же именем тоже принимаем, чтобы форму можно было упростить."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        names = value.get("choice_names")
        if isinstance(names, list) and names:
            return str(names[0])
        if value.get("choice_name"):
            return str(value["choice_name"])
    return None


def _is_rejected(task: dict[str, Any]) -> bool:
    for step in task.get("approvals") or []:
        for approval in step or []:
            if approval.get("approval_choice") == "rejected":
                return True
    return False


def _is_closed(task: dict[str, Any]) -> bool:
    closed = False
    for comment in task.get("comments") or []:
        action = comment.get("action")
        if action == "finished":
            closed = True
        elif action == "reopened":
            closed = False
    return closed or bool(task.get("close_date"))


def is_closed(task: dict[str, Any]) -> bool:
    return _is_closed(task)


def derive(task: dict[str, Any]) -> TaskState:
    state = _derive_electronic(task)
    paper_choice = _choice_name(field_value(task, PAPER_FIELD))
    paper = CHOICE_TO_PAPER.get(paper_choice.strip().lower()) if paper_choice else None
    return TaskState(state.status, state.message, paper)


def _derive_electronic(task: dict[str, Any]) -> TaskState:
    message = field_value(task, MESSAGE_FIELD)
    message = message.strip() if isinstance(message, str) and message.strip() else None

    if _is_rejected(task):
        return TaskState(RequestStatus.REJECTED, message)
    choice = _choice_name(field_value(task, STATUS_FIELD))
    explicit = CHOICE_TO_STATUS.get(choice.strip().lower()) if choice else None
    if explicit is not None and explicit != RequestStatus.IN_REVIEW:
        return TaskState(explicit, message)
    if _is_closed(task):
        return TaskState(RequestStatus.READY, message)
    return TaskState(RequestStatus.IN_REVIEW, message)
