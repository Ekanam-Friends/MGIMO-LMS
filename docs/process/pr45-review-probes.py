"""Локальные воспроизведения замечаний ревью; не продуктовые тесты."""
from datetime import date, datetime, time
import json

from app.assistant.personal import collect, collect_teaching
from app.assistant.routing import normalize_scopes, resolve_reference
from app.models import Lesson, LessonGroup, FinalGrade
from tests.test_web import client
from tests.test_assistant_teaching import teacher_fixture


def test_mixed_scopes_are_lost():
    assert normalize_scopes("Покажи мои домашние задания и расписание на завтра", ["homework", "schedule"]) == ["homework"]
    assert normalize_scopes("Какие условия стипендии и статус заявки на справку?", ["public", "requests"]) == ["public"]


def test_reference_skips_more_recent_topic():
    assert resolve_reference("Когда её получать?", ["Какие условия стипендии?", "Я заказал справку об обучении"]) == "Когда стипендии получать?"
    assert resolve_reference("Кто ведет математику и как его зовут?", []) is None


async def test_nearest_non_language_subject_is_removed(client, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 5, 8, 0, tzinfo=tz)
    monkeypatch.setattr("app.assistant.personal.datetime", Clock)
    async with client._transport.app.state.session_factory() as db:
        db.add(Lesson(id=300, date=date(2026, 11, 6), begins_at=time(9), ends_at=time(10, 20),
                      discipline="Математика", first_seen_at=datetime(2026, 11, 5), last_seen_at=datetime(2026, 11, 5)))
        await db.flush()
        db.add(LessonGroup(lesson_id=300, group_id=10))
        await db.commit()
        facts = await collect(db, 1, ["schedule"], date(2026, 11, 5), date(2026, 11, 5), date(2026, 11, 11), question="Когда у меня математика?")
    text = json.dumps(facts, ensure_ascii=False)
    assert "Своя дисциплина" in text
    assert "Математика" not in text


async def test_teacher_gets_conflicting_finals_without_attempt(client):
    await teacher_fixture(client)
    async with client._transport.app.state.session_factory() as db:
        db.add_all([
            FinalGrade(course_id=1, student_id=1, attempt=1, result="2", total_points=30, graded_on=date(2026, 11, 1)),
            FinalGrade(course_id=1, student_id=1, attempt=2, result="4", total_points=80, graded_on=date(2026, 11, 2)),
        ])
        await db.commit()
        facts = await collect_teaching(db, 1, 1, ["grades"], date(2026, 11, 20), date(2026, 11, 20), date(2026, 11, 26))
    finals = [fact["text"] for fact in facts if ": итог " in fact["text"]]
    assert len(finals) == 2
    assert all("попыт" not in fact for fact in finals)
