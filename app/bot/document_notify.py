"""Очередь уведомлений о документах: фиксация попытки до сети, результата после ответа."""
from datetime import timedelta
from html import escape

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import and_, or_, select, update

from app.auth.service import utcnow
from app.models import TelegramLink
from app.models.document_notice import DocumentNotice, DocumentRecipient
from app.models.knowledge import KnowledgeDocument


async def deliver_document_notifications(bot, sf, *, now=None, limit=50):
    now = now or utcnow()
    async with sf() as db:
        await db.execute(update(DocumentRecipient).where(
            DocumentRecipient.telegram_state == "sending", DocumentRecipient.telegram_attempts >= 5,
            DocumentRecipient.telegram_attempted_at < now - timedelta(minutes=5)).values(telegram_state="failed"))
        await db.commit()
    for _ in range(limit):
        async with sf() as db:
            row = await db.scalar(select(DocumentRecipient).join(DocumentNotice,
                DocumentNotice.document_id == DocumentRecipient.document_id).where(
                    DocumentNotice.confirmed_at >= now - timedelta(days=45),
                    DocumentRecipient.telegram_attempts < 5,
                    or_(and_(DocumentRecipient.telegram_state == "pending",
                             or_(DocumentRecipient.telegram_attempted_at.is_(None),
                                 DocumentRecipient.telegram_attempted_at <= now - timedelta(seconds=60))),
                        and_(DocumentRecipient.telegram_state == "sending",
                             DocumentRecipient.telegram_attempted_at <= now - timedelta(minutes=5))))
                .order_by(DocumentRecipient.document_id, DocumentRecipient.student_id)
                .limit(1).with_for_update(skip_locked=True))
            if row is None:
                break
            # Не отправлять старому владельцу после отвязки/перепривязки Telegram.
            linked = await db.scalar(select(TelegramLink).where(
                TelegramLink.student_id == row.student_id, TelegramLink.telegram_id == row.telegram_id))
            if linked is None:
                row.telegram_state = "skipped"
                await db.commit()
                continue
            title, url = (await db.execute(select(KnowledgeDocument.title, KnowledgeDocument.source_url)
                .where(KnowledgeDocument.id == row.document_id))).one()
            row.telegram_state = "sending"
            row.telegram_attempts += 1
            row.telegram_attempted_at = now
            await db.commit()
            try:
                await bot.send_message(row.telegram_id,
                    f"📄 <b>Новый документ</b>\n{escape(title)}\n\n"
                    f'Деканат отметил, что документ касается вас.\n<a href="{escape(url, quote=True)}">Открыть источник</a>',
                    parse_mode="HTML")
                row.telegram_state, row.telegram_sent_at = "sent", now
            except TelegramForbiddenError:
                row.telegram_state = "skipped"
            except TelegramAPIError:
                row.telegram_state = "failed" if row.telegram_attempts >= 5 else "pending"
            await db.commit()
