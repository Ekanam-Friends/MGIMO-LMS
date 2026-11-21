"""Параметры чтения расписания, независимые от текста вопроса и HTTP."""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScheduleQuery:
    discipline: str | None = None
    nearest: bool = False
    now: datetime | None = None
