from datetime import datetime, time, timedelta
import pytest
from sqlalchemy import select
from tests.test_grades import client, login
from app.models import Assessment, Lesson, LessonGroup, User, lesson_teachers
from app.models.learning import Submission, TaskMessage
from app import learning

@pytest.fixture(autouse=True)
async def homework(client):
    async with client.sf() as db:
        a=await db.get(Assessment,1); a.kind='homework'
        await db.commit()

async def test_submission_file_and_privacy(client):
    csrf=await login(client,'anna')
    r=await client.post('/learning/tasks/1/submit',data={'csrf':csrf,'text':'Моё решение'},files={'file':('answer.txt',b'answer')})
    assert r.status_code==303
    assert 'Моё решение' in (await client.get('/learning/tasks/1')).text
    assert (await client.get('/learning/tasks/1/files/1')).content==b'answer'
    await login(client,'boris')
    for path in ('/learning/tasks/1','/learning/tasks/1/files/1'):
        assert (await client.get(path)).status_code==404
    await login(client,'teacher')
    assert (await client.get('/learning/tasks/1/files/1')).content==b'answer'

async def test_review_and_private_messages(client):
    csrf=await login(client,'anna')
    assert (await client.post('/learning/tasks/1/submit',data={'csrf':csrf,'text':'Решение'})).status_code==303
    csrf=await login(client,'teacher')
    assert (await client.post('/learning/tasks/1/messages',data={'csrf':csrf,'body':'Личный ответ','student_id':1})).status_code==303
    assert (await client.post('/learning/tasks/1/review',data={'csrf':csrf,'student_id':1,'revision':1,'status':'reviewed','points':'8','comment':'Проверено'})).status_code==303
    await login(client,'anna')
    text=(await client.get('/learning/tasks/1')).text
    assert 'Личный ответ' in text and '8.0' in text
    assert 'Сообщение по заданию' in (await client.get('/notifications')).text
    await login(client,'boris')
    assert 'Личный ответ' not in (await client.get('/notifications')).text

async def test_empty_submission_and_csrf(client):
    csrf=await login(client,'anna')
    assert (await client.post('/learning/tasks/1/submit',data={'text':'x'})).status_code==403
    assert (await client.post('/learning/tasks/1/submit',data={'csrf':csrf,'text':''})).status_code==400

async def test_stale_review_rejected(client):
    csrf=await login(client,'anna')
    for text in ('Первая','Вторая'):
        assert (await client.post('/learning/tasks/1/submit',data={'csrf':csrf,'text':text})).status_code==303
    csrf=await login(client,'teacher')
    assert (await client.post('/learning/tasks/1/review',data={'csrf':csrf,'student_id':1,'revision':1,'status':'reviewed','points':'8'})).status_code==400

async def test_attendance_expiry_correction_and_attempts(client):
    now=datetime(2026,11,22,9,0)
    async with client.sf() as db:
        db.add(Lesson(id=999,date=now.date(),begins_at=time(11),ends_at=time(13),discipline='Тестовая пара',first_seen_at=now,last_seen_at=now))
        await db.flush()
        db.add(LessonGroup(lesson_id=999,group_id=10))
        await db.execute(lesson_teachers.insert().values(lesson_id=999,teacher_id=1)); await db.commit()
        teacher=await db.scalar(select(User).where(User.login=='teacher'))
        student=await db.scalar(select(User).where(User.login=='anna'))
        code,expires=await learning.open_attendance(db,teacher,999,'012345',now=now)
        assert expires-now==timedelta(seconds=90)
        with pytest.raises(learning.Invalid):
            await learning.check_in(db,student,999,code,now=expires)
        await learning.check_in(db,student,999,code,now=expires-timedelta(microseconds=1))
        await learning.correct_attendance(db,teacher,999,1,False,'Исправлено после сверки')
        with pytest.raises(learning.Invalid):
            await learning.check_in(db,student,999,code,now=now+timedelta(seconds=50))
        await db.commit()

async def test_telegram_delivery_after_success(client):
    from unittest.mock import AsyncMock
    from app.bot.learning_notify import deliver_learning_notifications
    from app.models import TelegramLink
    from app.models.learning import LearningNotice
    from app.auth.service import utcnow
    async with client.sf() as db:
        user=await db.scalar(select(User).where(User.login=='anna'))
        db.add(TelegramLink(student_id=1,telegram_id=12345,feed_token='test',linked_at=utcnow()))
        db.add(LearningNotice(user_id=user.id,title='Тестовое сообщение',body='Текст',href='/learning/tasks/1',created_at=utcnow()))
        await db.commit()
    bot=AsyncMock()
    await deliver_learning_notifications(bot,client.sf)
    assert bot.send_message.await_count==1
    await deliver_learning_notifications(bot,client.sf)
    assert bot.send_message.await_count==1

async def test_assistant_reads_submission_status(client):
    from app.assistant.personal import collect
    from datetime import date
    csrf=await login(client,'anna')
    await client.post('/learning/tasks/1/submit',data={'csrf':csrf,'text':'Решение'})
    async with client.sf() as db:
        facts=await collect(db,1,['homework'],date.today(),date.today(),date.today())
        assert any('Ждёт проверки' in f['text'] for f in facts)
        other=await collect(db,2,['homework'],date.today(),date.today(),date.today())
        assert not any('Ждёт проверки' in f['text'] for f in other)


async def test_graded_submission_can_only_be_replaced_after_return(client):
    url = "/learning/tasks/1"
    csrf = await login(client, "anna")
    assert (await client.post(url + "/submit", data={"csrf": csrf, "text": "Первая версия"})).status_code == 303
    csrf = await login(client, "teacher")
    review = {"csrf": csrf, "student_id": 1, "revision": 1, "status": "reviewed", "points": "8"}
    assert (await client.post(url + "/review", data=review)).status_code == 303

    csrf = await login(client, "anna")
    page = (await client.get(url)).text
    assert "Повторная отправка доступна после возврата на доработку" in page
    assert "Отправить решение" not in page
    assert (await client.post(url + "/submit", data={"csrf": csrf, "text": "Вторая версия"})).status_code == 400
    assert "Первая версия" in (await client.get(url)).text

    csrf = await login(client, "teacher")
    review.update(csrf=csrf, status="returned", comment="Добавьте обоснование")
    assert (await client.post(url + "/review", data=review)).status_code == 303
    csrf = await login(client, "anna")
    assert (await client.post(url + "/submit", data={"csrf": csrf, "text": "Вторая версия"})).status_code == 303
    page = (await client.get(url)).text
    assert "Ждёт проверки · версия 2" in page
    assert "Последняя оценка" not in page


async def test_telegram_failure_does_not_block_next_event(client, monkeypatch):
    from unittest.mock import AsyncMock
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
    from aiogram.methods import SendMessage
    from app.auth.service import utcnow
    from app.bot import learning_notify
    from app.models import TelegramLink
    from app.models.learning import LearningNotice

    now = utcnow()
    monkeypatch.setattr(learning_notify, "utcnow", lambda: now)
    async with client.sf() as db:
        for sid, login_name in [(1, "anna"), (2, "boris")]:
            uid = await db.scalar(select(User.id).where(User.login == login_name))
            db.add(TelegramLink(student_id=sid, telegram_id=100 + sid, feed_token=f"test-{sid}", linked_at=now))
            db.add(LearningNotice(user_id=uid, title="Задание", body="Текст", href="/learning/tasks/1", created_at=now))
        await db.commit()

    message = SendMessage(chat_id=101, text="Текст")
    bot = AsyncMock()
    bot.send_message.side_effect = [TelegramNetworkError(method=message, message="Сбой"), None]
    await learning_notify.deliver_learning_notifications(bot, client.sf)
    await learning_notify.deliver_learning_notifications(bot, client.sf)
    assert [call.args[0] for call in bot.send_message.await_args_list] == [101, 102]

    now += learning_notify.RETRY_DELAY
    bot.send_message.side_effect = TelegramBadRequest(method=message, message="chat not found")
    await learning_notify.deliver_learning_notifications(bot, client.sf)
    await learning_notify.deliver_learning_notifications(bot, client.sf)
    assert [call.args[0] for call in bot.send_message.await_args_list] == [101, 102, 101]
    async with client.sf() as db:
        notices = (await db.scalars(select(LearningNotice).order_by(LearningNotice.id))).all()
        assert [(n.telegram_state, n.attempts) for n in notices] == [("failed", 2), ("sent", 1)]
        assert notices[0].sent_at is None and notices[1].sent_at is not None


async def test_telegram_delivers_only_grade_events_queued_by_grades_service(client):
    from unittest.mock import AsyncMock
    from app.auth.service import utcnow
    from app.bot.learning_notify import deliver_learning_notifications
    from app.grades.service import set_score
    from app.models import GradeEvent, TelegramLink
    from app.models.grades import GradeEventKind

    now = utcnow()
    async with client.sf() as db:
        db.add(TelegramLink(student_id=1, telegram_id=101, feed_token="test", linked_at=now))
        db.add(GradeEvent(student_id=1, course_id=1, kind=GradeEventKind.HOMEWORK,
                          text="Старое задание", created_at=now - timedelta(days=60)))
        await db.flush()
        await set_score(db, 1, 1, 1, 1, "8")
        await db.commit()

    bot = AsyncMock()
    await deliver_learning_notifications(bot, client.sf)
    await deliver_learning_notifications(bot, client.sf)
    assert bot.send_message.await_count == 1
    assert "8 из 10" in bot.send_message.await_args.args[1]
    async with client.sf() as db:
        old, new = (await db.scalars(select(GradeEvent).order_by(GradeEvent.id))).all()
        assert old.notified_at is None and new.notified_at is not None


async def test_reset_removes_learning_dependencies_with_foreign_keys_enabled(client):
    from sqlalchemy import text
    from app.auth.service import utcnow
    from app.generate.run import GEN_PREFIX, reset
    from app.models import Student
    from app.models.learning import Attendance, AttendanceAttempt, AttendanceChange, LearningNotice

    csrf = await login(client, "anna")
    assert (await client.post("/learning/tasks/1/submit", data={"csrf": csrf, "text": "Решение"})).status_code == 303
    assert (await client.post("/learning/tasks/1/messages", data={"csrf": csrf, "body": "Вопрос"})).status_code == 303
    now = utcnow()
    async with client.sf() as db:
        await db.execute(text("PRAGMA foreign_keys=ON"))
        student = await db.get(Student, 1)
        student.record_book = f"{GEN_PREFIX}TEST"
        user = await db.scalar(select(User).where(User.login == "anna"))
        db.add(Lesson(id=999, date=now.date(), begins_at=time(9), ends_at=time(10),
                      discipline="Тестовая пара", first_seen_at=now, last_seen_at=now))
        await db.flush()
        db.add_all([
            Attendance(lesson_id=999, student_id=1, present=True, updated_at=now),
            AttendanceAttempt(lesson_id=999, student_id=1, opened_at=now),
            AttendanceChange(lesson_id=999, student_id=1, actor_id=user.id, present=True, created_at=now),
            Attendance(lesson_id=999, student_id=2, present=True, updated_at=now),
        ])
        await db.commit()
        await reset(db)
        await db.commit()
        for model in (Assessment, Submission, TaskMessage, LearningNotice, AttendanceAttempt, AttendanceChange):
            assert await db.scalar(select(model)) is None
        assert await db.get(Student, 1) is None
        assert await db.get(Attendance, (999, 1)) is None
        assert await db.get(Attendance, (999, 2)) is not None


async def test_existing_journal_score_blocks_first_submission(client):
    from app.grades.service import set_score

    async with client.sf() as db:
        await set_score(db, 1, 1, 1, 1, "0")
        await db.commit()
    csrf = await login(client, "anna")
    url = "/learning/tasks/1"
    assert "В ведомости уже есть оценка" in (await client.get(url)).text
    assert (await client.post(url + "/submit", data={"csrf": csrf, "text": "Решение"})).status_code == 400
