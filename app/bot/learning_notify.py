"""Доставляем учебные события и повторяем временные сбои по расписанию."""
from datetime import timedelta

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import or_, select

from app.auth.service import utcnow
from app.models import GradeEvent, TelegramLink, User
from app.models.learning import LearningNotice

RETRY_DELAY = timedelta(minutes=5)


async def deliver_learning_notifications(bot, sf, limit=50):
    for model in (LearningNotice, GradeEvent):
        for _ in range(limit):
            async with sf() as db:
                now = utcnow()
                due = model.telegram_retry_at <= now
                if model is LearningNotice:
                    eligible = (model.telegram_state == "pending") & (or_(model.telegram_retry_at.is_(None), due))
                else:
                    # Старые события без явной постановки в очередь остаются в ленте ЛК.
                    eligible = model.notified_at.is_(None) & due
                query = select(model).where(eligible).order_by(model.id).limit(1).with_for_update(skip_locked=True)
                row = await db.scalar(query)
                if row is None:
                    break

                is_notice = isinstance(row, LearningNotice)
                student_id = (await db.scalar(select(User.student_id).where(User.id == row.user_id))
                              if is_notice else row.student_id)
                link = (await db.scalar(select(TelegramLink).where(TelegramLink.student_id == student_id))
                        if student_id is not None else None)
                state, retry_at = "skipped", None
                if link is not None:
                    if is_notice:
                        row.attempts += 1
                        row.attempted_at = now
                        text = f"{row.title}\n{row.body}\nОткройте уведомление в личном кабинете."
                    else:
                        text = row.text
                    try:
                        await bot.send_message(link.telegram_id, text[:3500], parse_mode=None)
                    except TelegramForbiddenError:
                        pass  # Пользователь отключил бота.
                    except TelegramBadRequest:
                        state = "failed"
                    except TelegramRetryAfter as exc:
                        retry_at = utcnow() + timedelta(seconds=exc.retry_after)
                    except TelegramAPIError:
                        retry_at = utcnow() + RETRY_DELAY
                    else:
                        state = "sent"

                row.telegram_retry_at = retry_at
                if is_notice:
                    row.telegram_state = "pending" if retry_at is not None else state
                    row.sent_at = utcnow() if state == "sent" else None
                elif retry_at is None:
                    row.notified_at = utcnow()
                await db.commit()
