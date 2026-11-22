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
