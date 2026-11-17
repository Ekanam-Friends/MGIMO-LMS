"""Доменная беседа: разрешение на оценки выдаётся только после явного запроса."""
from dataclasses import dataclass, field


@dataclass
class Conversation:
    owner: str
    expires: float
    _grades_allowed: bool = field(default=False, init=False, repr=False)
    _grades_pending: bool = field(default=False, init=False, repr=False)
    student_id: int | None = None
    actor_role: str = "student"
    language: str = "ru"
    teacher_id: int | None = None

    @property
    def grades_allowed(self) -> bool:
        return self._grades_allowed

    @property
    def grades_pending(self) -> bool:
        return self._grades_pending

    def request_grades(self) -> None:
        if not self._grades_allowed:
            self._grades_pending = True

    def grant_grades(self) -> None:
        if not self._grades_pending:
            raise ValueError("Разрешение не запрошено беседой")
        self._grades_allowed, self._grades_pending = True, False
