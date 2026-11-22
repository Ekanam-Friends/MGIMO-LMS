import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncio, tempfile
from pathlib import Path
import uvicorn
from playwright.async_api import async_playwright
from tests.test_grades import client, PASSWORD
from app.models import Assessment, Lesson, LessonGroup, lesson_teachers
from datetime import datetime,time
async def main():
 gen=client.__wrapped__(Path(tempfile.mkdtemp()))
 c=await gen.__anext__()
 async with c.sf() as db:
  a=await db.get(Assessment,1);a.kind='homework'
  db.add(Lesson(id=555,date=datetime.now().date(),begins_at=time(9),ends_at=time(10),discipline='Тестовая пара',first_seen_at=datetime.now(),last_seen_at=datetime.now()))
  await db.flush();db.add(LessonGroup(lesson_id=555,group_id=10))
  await db.execute(lesson_teachers.insert().values(lesson_id=555,teacher_id=1));await db.commit()
 server=uvicorn.Server(uvicorn.Config(c._transport.app,host='127.0.0.1',port=8768,lifespan='off',log_level='error'))
 task=asyncio.create_task(server.serve())
 while not server.started:await asyncio.sleep(.1)
 try:
  async with async_playwright() as pw:
   browser=await pw.chromium.launch(headless=True,executable_path='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
   page=await browser.new_page(viewport={'width':360,'height':800})
   await page.goto('http://127.0.0.1:8768/login')
   await page.locator('[name=login]').fill('anna');await page.locator('[name=password]').fill(PASSWORD)
   await page.locator('button[type=submit]').click()
   for suffix,name in [('/learning/tasks','tasks'),('/learning/tasks/1','task'),('/attendance','attendance')]:
    await page.goto('http://127.0.0.1:8768'+suffix)
    assert await page.locator('body').inner_text()
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth'),suffix
    await page.screenshot(path=f'docs/process/screenshots/2026-11-22-{name}-360.png',full_page=True)
   await page.goto('http://127.0.0.1:8768/learning/tasks/1')
   await page.locator('textarea[name=text]').fill('Тестовое решение через форму')
   await page.get_by_role('button',name='Отправить решение',exact=True).click()
   assert 'Ждёт проверки' in await page.locator('body').inner_text()
   await page.screenshot(path='docs/process/screenshots/2026-11-22-submitted-360.png',full_page=True)
   await page.context.clear_cookies()
   await page.goto('http://127.0.0.1:8768/login')
   await page.locator('[name=login]').fill('teacher');await page.locator('[name=password]').fill(PASSWORD)
   await page.locator('button[type=submit]').click()
   await page.goto('http://127.0.0.1:8768/learning/tasks/1')
   assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
   await page.screenshot(path='docs/process/screenshots/2026-11-22-teacher-review-360.png',full_page=True)
   await page.locator('[name=points]').fill('8')
   await page.locator('[name=comment]').fill('Принято после проверки')
   await page.get_by_role('button',name='Оценить',exact=True).click()
   assert 'Проверено' in await page.locator('body').inner_text()
   await page.goto('http://127.0.0.1:8768/attendance/555')
   assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
   await page.screenshot(path='docs/process/screenshots/2026-11-22-teacher-attendance-360.png',full_page=True)
   await browser.close()
   print('Mobile login, screens and submission verified at 360px')
 finally:
  server.should_exit=True;await task
  await gen.aclose()
asyncio.run(main())
