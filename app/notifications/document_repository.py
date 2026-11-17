"""SQL-адаптер рассылки. Получатели и подтверждение сохраняются одной транзакцией."""
import hashlib
import json
from datetime import timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.models import Student, StudyGroup, TelegramLink
from app.models.document_notice import DocumentNotice, DocumentRecipient
from .document_domain import AudienceOption, NoticeDraft, NoticeError, fingerprint, validate_targets


def token(kind, text):
    return kind + ":" + hashlib.sha256(text.encode()).hexdigest()[:24]


class SqlNoticeRepository:
    def __init__(self, db):
        self.db = db

    async def _audiences(self):
        result = [(AudienceOption("all", "Все студенты"), Student.id.is_not(None))]
        faculties = (await self.db.scalars(select(Student.faculty_name).where(Student.faculty_name != "")
            .distinct().order_by(Student.faculty_name))).all()
        for name in faculties:
            result.append((AudienceOption(token("faculty", name), "Факультет: " + name), Student.faculty_name == name))
        groups = (await self.db.scalars(select(StudyGroup).order_by(StudyGroup.number, StudyGroup.id))).all()
        for group in groups:
            result.append((AudienceOption(f"group:{group.id}", "Группа: " + group.number), Student.study_group_id == group.id))
        streams = (await self.db.execute(select(Student.speciality, Student.course).where(Student.speciality != "")
            .distinct().order_by(Student.speciality, Student.course))).all()
        for program, year in streams:
            result.append((AudienceOption(token("stream", json.dumps([program, year], ensure_ascii=False)),
                f"Поток: {program}, {year} курс"), and_(Student.speciality == program, Student.course == year)))
        return result

    async def options(self):
        return [option for option, _ in await self._audiences()]

    async def recipients(self, targets):
        validate_targets(targets)
        options = {option.key: predicate for option, predicate in await self._audiences()}
        conditions = []
        for key in targets:
            if key in options:
                conditions.append(options[key])
            elif key.startswith("student:") and key[8:].isdigit():
                sid = int(key[8:])
                if not await self.db.get(Student, sid):
                    raise NoticeError("Выбранный студент не найден")
                conditions.append(Student.id == sid)
            else:
                raise NoticeError("Аудитория изменилась или не существует. Выберите адресатов заново")
        return list((await self.db.scalars(select(Student.id).where(or_(*conditions)).order_by(Student.id))).all())

    async def _row(self, document_id):
        row = await self.db.get(DocumentNotice, document_id)
        if row is None:
            try:
                async with self.db.begin_nested():
                    row = DocumentNotice(document_id=document_id, suggested_targets=[], explanation="",
                        preview_targets=[], preview_count=0)
                    self.db.add(row)
                    await self.db.flush()
            except IntegrityError:
                row = await self.db.get(DocumentNotice, document_id, populate_existing=True)
        return row

    async def draft(self, document_id):
        row = await self._row(document_id)
        return NoticeDraft(row.confirmed_at, row.preview_token, row.preview_by, row.preview_sha256,
            row.preview_at, list(row.preview_targets), row.preview_recipients_hash)

    async def claim_suggestion(self, document_id, now):
        await self._row(document_id)
        claimed = await self.db.execute(update(DocumentNotice).where(
            DocumentNotice.document_id == document_id, DocumentNotice.confirmed_at.is_(None),
            or_(DocumentNotice.suggestion_at.is_(None), DocumentNotice.suggestion_at < now - timedelta(seconds=30)))
            .values(suggestion_at=now))
        await self.db.commit()
        return bool(claimed.rowcount)

    async def save_suggestion(self, document_id, now, targets, explanation):
        await self.db.execute(update(DocumentNotice).where(DocumentNotice.document_id == document_id,
            DocumentNotice.confirmed_at.is_(None), DocumentNotice.suggestion_at == now).values(
                suggested_targets=targets, explanation=explanation))
        await self.db.commit()

    async def save_preview(self, document_id, actor_id, document_sha256, targets, ids, token, now):
        await self._row(document_id)
        result = await self.db.execute(update(DocumentNotice).where(DocumentNotice.document_id == document_id,
            DocumentNotice.confirmed_at.is_(None)).values(preview_targets=targets,
                preview_token=token, preview_sha256=document_sha256,
                preview_recipients_hash=fingerprint(ids), preview_count=len(ids), preview_by=actor_id, preview_at=now))
        if not result.rowcount:
            raise NoticeError("Уведомление уже подтверждено")
        await self.db.commit()

    async def confirm(self, document_id, actor_id, token, ids, now):
        result = await self.db.execute(update(DocumentNotice).where(DocumentNotice.document_id == document_id,
            DocumentNotice.confirmed_at.is_(None), DocumentNotice.preview_token == token).values(
                confirmed_at=now, confirmed_by=actor_id))
        if not result.rowcount:
            await self.db.rollback()
            current = await self.db.get(DocumentNotice, document_id, populate_existing=True)
            if current and current.confirmed_at:
                return False
            raise NoticeError("Предпросмотр изменён другим работником. Проверьте получателей заново")
        links = dict((await self.db.execute(select(TelegramLink.student_id, TelegramLink.telegram_id)
            .where(TelegramLink.student_id.in_(ids)))).all())
        self.db.add_all([DocumentRecipient(document_id=document_id, student_id=sid, telegram_id=links.get(sid),
            telegram_state="pending" if sid in links else "skipped", telegram_attempts=0) for sid in ids])
        await self.db.commit()
        return True
