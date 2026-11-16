"""Разрешения беседы в памяти одного процесса; перезапуск отзывает разрешения."""
from dataclasses import dataclass
import secrets
import time


@dataclass
class Conversation:
    owner: str
    expires: float
    grades_allowed: bool = False
    grades_pending: bool = False
    student_id: int | None = None
    actor_role: str = "student"
    language: str = "ru"


class Conversations:
    def __init__(self):
        self.items: dict[str, Conversation] = {}

    def start(self, owner: str, student_id: int | None = None, actor_role: str = "student", language: str = "ru") -> str:
        now = time.monotonic()
        self.items = {k: v for k, v in self.items.items() if v.expires > now}
        if len(self.items) >= 2000:
            raise RuntimeError("Слишком много бесед")
        token = secrets.token_urlsafe(32)
        self.items[token] = Conversation(owner, now + 2 * 60 * 60, student_id=student_id, actor_role=actor_role, language=language)
        return token

    def get(self, token: str, owner: str) -> Conversation | None:
        row = self.items.get(token)
        if row and row.owner == owner and row.expires > time.monotonic():
            return row
        return None

    def clear(self, token: str, owner: str):
        if self.get(token, owner):
            del self.items[token]
