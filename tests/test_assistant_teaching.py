"""Преподаватель видит своих студентов, только свои дисциплины и дату загрузки."""
from datetime import date, datetime
import json

from sqlalchemy import delete, update

from app.assistant.personal import collect_teaching
from app.models import Assessment, Course, CourseEnrollment, Discipline, Score, Teacher
from app.models.auth import User
from tests.test_web import client, login


async def teacher_fixture(client):
    async with client._transport.app.state.session_factory() as db:
        db.add(Teacher(id=1, ruz_oid=101, full_name="Вымышленов Учитель Тестович"))
        db.add_all([Discipline(id=1, name="Своя математика"), Discipline(id=2, name="Другой предмет")])
        await db.flush()
        db.add_all([Course(id=1, discipline_id=1, group_id=10, teacher_id=1, semester=1,
                           academic_year="2026/2027", created_at=datetime(2026, 9, 1)),
                    Course(id=2, discipline_id=2, group_id=10, semester=1,
                           academic_year="2026/2027", created_at=datetime(2026, 9, 1))])
        await db.flush()
        db.add_all([CourseEnrollment(course_id=1, student_id=1), CourseEnrollment(course_id=2, student_id=1),
                    Assessment(id=1, course_id=1, title="Своя работа", max_points=10, created_at=datetime(2026, 9, 1)),
                    Assessment(id=2, course_id=2, title="Скрытая работа", max_points=10, created_at=datetime(2026, 9, 1))])
        await db.flush()
        db.add_all([Score(assessment_id=1, student_id=1, points=7, graded_at=datetime(2026, 11, 1)),
                    Score(assessment_id=2, student_id=1, points=9, graded_at=datetime(2026, 11, 1))])
        await db.execute(update(User).where(User.login == "boris").values(role="teacher", student_id=None, teacher_id=1))
        await db.commit()


async def test_teacher_picker_and_conversation_reject_foreign_student_even_with_demo_access(client):
    csrf = await login(client, "boris")
    await teacher_fixture(client)
    headers = {"X-CSRF-Token": csrf}
    app = client._transport.app
    page = await client.get("/s/assistant")
    assert 'id="assistant-student-search"' not in page.text
    for demo in (False, True):
        app.state.assistant.cfg.assistant_demo_student_access = demo
        response = await client.get("/s/assistant/students")
        assert response.status_code == 200 and [row["id"] for row in response.json()["students"]] == [1]
        denied = await client.post("/s/assistant/conversation", json={"action": "start", "acknowledged": True,
            "student_id": 2}, headers=headers)
        assert denied.status_code == 404
    started = await client.post("/s/assistant/conversation", json={"action": "start", "acknowledged": True,
        "student_id": 1}, headers=headers)
    assert started.status_code == 200
    assert (await client.get("/s/assistant/students/2/data-status")).status_code == 404
    status = await client.get("/s/assistant/students/1/data-status")
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json()["scores"] == 1
    assert started.json()["data_status"] == {"courses": 1, "assessments": 1, "scores": 1, "schedule_imported_at": None}
    token = started.json()["conversation_token"]
    async with app.state.session_factory() as db:
        await db.execute(delete(CourseEnrollment).where(CourseEnrollment.course_id == 1))
        await db.commit()
    # Уже открытая беседа не сохраняет доступ после изменения зачислений.
    response = await client.post("/s/assistant/ask", json={"question": "Какие оценки?",
        "conversation_token": token}, headers=headers)
    assert response.status_code == 404


async def test_teacher_facts_never_include_other_subject_or_certificate(client):
    await teacher_fixture(client)
    async with client._transport.app.state.session_factory() as db:
        facts = await collect_teaching(db, 1, 1, ["grades", "schedule", "requests"],
                                      date(2026, 11, 17), date(2026, 11, 1), date(2026, 11, 30))
    text = json.dumps(facts, ensure_ascii=False)
    assert "Своя математика" in text and "7 из 10" in text
    assert "Скрытая работа" not in text and "Другой предмет" not in text and "9 из 10" not in text
    assert "секрет" not in text and "не входят в область" in text
    assert "не подтверждено" in text


async def test_teacher_llm_receives_only_scoped_facts(client, tmp_path):
    from app.assistant.conversation import Conversation
    from app.assistant.personal import SqlTeachingEvidenceReader
    from app.assistant.bootstrap import create_assistant as Assistant
    from tests.test_assistant import FakeProvider, config
    await teacher_fixture(client)
    provider = FakeProvider({"scopes": ["grades"]}, {"fact_ids": ["fact-2"]})
    assistant = Assistant(config(tmp_path), provider)
    conversation = Conversation("owner", float("inf"), actor_role="teacher", teacher_id=1)
    conversation.request_grades()
    conversation.grant_grades()
    async with client._transport.app.state.session_factory() as db:
        result = await assistant.chat(100, None, "Какие баллы у моих студентов?", [], None, conversation, SqlTeachingEvidenceReader(db, 1))
    assert result["status"] == "answer" and "7 из 10" in result["facts"][0]["text"]
    payload = json.dumps(provider.calls, ensure_ascii=False)
    assert "Своя математика" in payload and "Скрытая работа" not in payload
    assert "Другой предмет" not in payload and "секрет" not in payload
