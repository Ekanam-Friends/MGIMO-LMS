"""Доменная беседа: разрешение на оценки выдаётся только после явного запроса."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Turn:
    question: str
    answer: str
    contains_grades: bool = False


@dataclass
class Conversation:
    owner: str
    expires: float
    _grades_allowed: bool = field(default=False, init=False, repr=False)
    _grades_pending: bool = field(default=False, init=False, repr=False)
    student_id: int | None = None
    actor_role: str = "student"
    language: str = "ru"
    _turns: list[Turn] = field(default_factory=list, init=False, repr=False)
    teacher_id: int | None = None

    @property
    def history(self) -> list[str]:
        return [turn.question for turn in self._turns[-4:]]

    def dialogue(self) -> list[dict]:
        """История — контекст беседы, а не доказательство актуальности данных."""
        return [message for turn in self._turns for message in (
            {"role": "user", "content": turn.question},
            {"role": "assistant", "content": turn.answer if not turn.contains_grades or self.grades_allowed
             else "[Ответ с оценками исключён: нет действующего разрешения]"},
        )]

    def remember(self, question: str, answer: str, *, contains_grades: bool = False) -> None:
        # Ограничиваем память целыми ходами, не оставляя ответ без его вопроса.
        self._turns.append(Turn(question[:2000], answer[:12000], contains_grades))
        while len(self._turns) > 8 or sum(len(t.question) + len(t.answer) for t in self._turns) > 24000:
            self._turns.pop(0)

    def clear(self) -> None:
        self._turns.clear()
        self._grades_allowed = self._grades_pending = False

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
