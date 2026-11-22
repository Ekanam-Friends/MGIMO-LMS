"""Доставка учебных событий. Сбой API сохраняет событие для повторной попытки."""
from html import escape
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import select
from app.auth.service import utcnow
from app.models import GradeEvent, TelegramLink, User
from app.models.learning import LearningNotice

async def deliver_learning_notifications(bot, sf, limit=50):
    for model in (LearningNotice, GradeEvent):
        for _ in range(limit):
            async with sf() as db:
                q = select(model)
                q = q.where(model.telegram_state == "pending") if model is LearningNotice else q.where(model.notified_at.is_(None))
                row = await db.scalar(q.order_by(model.id).limit(1).with_for_update(skip_locked=True))
                if row is None:
                    break
                sid = await db.scalar(select(User.student_id).where(User.id == row.user_id)) if model is LearningNotice else row.student_id
                link = await db.scalar(select(TelegramLink).where(TelegramLink.student_id == sid)) if sid else None
                if link:
                    text = row.title + "\n" + row.body + "\nОткройте задание в личном кабинете." if model is LearningNotice else row.text
                    try:
                        await bot.send_message(link.telegram_id, escape(text[:3500]), parse_mode="HTML")
                    except TelegramForbiddenError:
                        pass  # Пользователь отключил бота; очередь остальных не блокируется.
                    except TelegramAPIError:
                        await db.rollback()
                        break
                if model is LearningNotice:
                    row.telegram_state = "sent" if link else "skipped"
                    row.sent_at = utcnow() if link else None
                else:
                    row.notified_at = utcnow()
                await db.commit()
