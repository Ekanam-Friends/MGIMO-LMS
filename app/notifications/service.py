"""Лента колокольчика: объявления + статусы справок + новые баллы и ДЗ.

Ничего не копируется в отдельную таблицу уведомлений: события справок и баллов читаются из
их журналов в момент открытия ленты, объявления — из `announcements`. Всё фильтруется по
пользователю из сессии (чужое увидеть нельзя), прочитанное — `notification_reads`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import utcnow
from app.cache import cached, get_cache
from app.cache.invalidation import NOTIFICATIONS, reads_of
from app.models import (
    Announcement,
    CertificateRequest,
    CertificateRequestEvent,
    Course,
    Discipline,
    GradeEvent,
    NotificationRead,
    Student,
    StudyGroup,
)
from app.models.auth import Role, User
from app.models.certificates import CertificateKind, EventKind, PaperStatus, RequestStatus
from app.models.notifications import Audience, Source

LIMIT = 30
ALL_LIMIT = 1000
HORIZON = timedelta(days=45)
KINDS = {"ann", "cert", "grade", "doc"}
TITLE_MAX = 200
BODY_MAX = 2000


@dataclass
class Item:
    kind: str
    ref: int
    source: str
    title: str
    body: str
    created_at: datetime
    href: str | None = None
    unread: bool = True

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.ref}"

    @property
    def source_title(self) -> str:
        return Source.TITLES[self.source]


async def _announcements(db: AsyncSession, student: Student | None, since: datetime, limit: int,
                         source: str | None) -> list[Item]:
    q = select(Announcement).where(Announcement.created_at >= since)
    if source is not None:
        q = q.where(Announcement.source == source)
    if student is not None:
        cond = [Announcement.audience == Audience.ALL]
        if student.faculty_name:
            cond.append(and_(Announcement.audience == Audience.FACULTY,
                             Announcement.faculty_name == student.faculty_name))
        if student.study_group_id is not None:
            cond.append(and_(Announcement.audience == Audience.GROUP,
                             Announcement.group_id == student.study_group_id))
        if student.speciality:
            cond.append(and_(Announcement.audience == Audience.STREAM, Announcement.program == student.speciality,
                             Announcement.course == student.course))
        cond.append(and_(Announcement.audience == Audience.STUDENT, Announcement.student_id == student.id))
        q = q.where(or_(*cond))
    else:
        # Преподаватель: общие объявления деканата и вуза (адресные — студентам).
        q = q.where(Announcement.audience == Audience.ALL, Announcement.source != Source.TEACHER)
    rows = (await db.scalars(q.order_by(Announcement.created_at.desc()).limit(limit))).all()
    return [Item("ann", a.id, a.source, a.title, a.body, a.created_at) for a in rows]


def _cert_item(ev: CertificateRequestEvent, req: CertificateRequest) -> Item | None:
    what = CertificateKind.TITLES.get(req.kind, "Справка")
    href = f"/mfc/certificates/{req.id}"
    if ev.kind == EventKind.STATUS and ev.status and ev.status != RequestStatus.ACCEPTED:
        title = f"{what}: {RequestStatus.TITLES.get(ev.status, ev.status).lower()}"
        body = req.dean_message if ev.status == RequestStatus.NEED_INFO and req.dean_message else ""
    elif ev.kind == EventKind.DEAN_MESSAGE:
        title, body = f"Деканат написал по заявке: {what.lower()}", ev.text or ""
    elif ev.kind == EventKind.PDF_READY:
        title, body = f"{what}: можно скачать PDF", ""
    elif ev.kind == EventKind.PAPER and ev.status:
        title, body = f"{what}: {PaperStatus.TITLES.get(ev.status, ev.status).lower()}", ""
    else:
        return None
    return Item("cert", ev.id, Source.DEAN, title, body, ev.created_at, href)


async def _certificates(db: AsyncSession, student_id: int, since: datetime, limit: int) -> list[Item]:
    rows = (await db.execute(
        select(CertificateRequestEvent, CertificateRequest)
        .join(CertificateRequest, CertificateRequest.id == CertificateRequestEvent.request_id)
        .where(CertificateRequest.student_id == student_id, CertificateRequestEvent.created_at >= since)
        .order_by(CertificateRequestEvent.created_at.desc()).limit(limit * 2)
    )).all()
    return [i for ev, req in rows if (i := _cert_item(ev, req))]


async def _grades(db: AsyncSession, student_id: int, since: datetime, limit: int, *,
                  grade_details: bool) -> list[Item]:
    title = (Discipline.name + ": " + GradeEvent.text if grade_details else
             literal("Новое событие по успеваемости"))
    rows = (await db.execute(
        select(GradeEvent.id, GradeEvent.created_at, title)
        .select_from(GradeEvent)
        .join(Course, Course.id == GradeEvent.course_id)
        .join(Discipline, Discipline.id == Course.discipline_id)
        .where(GradeEvent.student_id == student_id, GradeEvent.created_at >= since)
        .order_by(GradeEvent.created_at.desc()).limit(limit)
    )).all()
    return [Item("grade", event_id, Source.TEACHER, heading, "", created_at, "/grades")
            for event_id, created_at, heading in rows]


async def _documents(db: AsyncSession, student_id: int, since: datetime, limit: int) -> list[Item]:
    from app.models.document_notice import DocumentNotice, DocumentRecipient
    from app.models.knowledge import KnowledgeDocument
    rows = (await db.execute(select(KnowledgeDocument.id, KnowledgeDocument.title,
        KnowledgeDocument.source_url, DocumentNotice.confirmed_at)
        .join(DocumentNotice, DocumentNotice.document_id == KnowledgeDocument.id)
        .join(DocumentRecipient, DocumentRecipient.document_id == KnowledgeDocument.id)
        .where(DocumentRecipient.student_id == student_id, DocumentNotice.confirmed_at >= since)
        .order_by(DocumentNotice.confirmed_at.desc()).limit(limit))).all()
    return [Item("doc", did, Source.DEAN, "Новый документ: " + title,
        "Деканат отметил, что документ касается вас. Откройте публичный источник.", at, url)
        for did, title, url, at in rows]


def _dump_items(items: list[Item]) -> list[dict]:
    return [{**asdict(i), "created_at": i.created_at.isoformat()} for i in items]


def _restore_items(raw: list[dict]) -> list[Item]:
    return [Item(**{**r, "created_at": datetime.fromisoformat(r["created_at"])}) for r in raw]


async def feed(db: AsyncSession, user: User, student: Student | None, *, now: datetime | None = None,
               limit: int = LIMIT, source: str | None = None, grade_details: bool = True) -> list[Item]:
    """Свежие уведомления пользователя, новые сверху, с отметкой прочитанного.

    Обычная лента (колокольчик на каждой странице) — через кэш (решение 0027). Ключ — id
    пользователя из сессии и его студента; поколения сбрасываются при новом объявлении, событии
    справки, балле, уведомлении о документе (общее) и при отметке «прочитано» (личное).
    """
    if now is not None or limit != LIMIT or source is not None or not grade_details:
        return await _load_feed(db, user, student, now=now, limit=limit, source=source, grade_details=grade_details)
    cache = get_cache()
    gens = await cache.generations(NOTIFICATIONS, reads_of(user.id))
    if gens is None:
        return await _load_feed(db, user, student, now=now, limit=limit)
    key = f"feed:u{user.id}:s{student.id if student else 0}:{gens[0]}:{gens[1]}"
    return await cached(key, cache.feed_ttl, lambda: _load_feed(db, user, student, now=now, limit=limit),
                        dump=_dump_items, restore=_restore_items)


async def _load_feed(db: AsyncSession, user: User, student: Student | None, *, now: datetime | None,
                     limit: int, source: str | None = None, grade_details: bool = True) -> list[Item]:
    since = (now or utcnow()) - HORIZON
    items = await _announcements(db, student, since, limit, source)
    if student is not None:
        if source in (None, Source.DEAN):
            items += await _certificates(db, student.id, since, limit)
            items += await _documents(db, student.id, since, limit)
        if source in (None, Source.TEACHER):
            items += await _grades(db, student.id, since, limit, grade_details=grade_details)
    items.sort(key=lambda i: i.created_at, reverse=True)
    items = items[:limit]
    if items:
        read = set((await db.scalars(
            select(NotificationRead.key).where(NotificationRead.user_id == user.id,
                                               NotificationRead.key.in_([i.key for i in items]))
        )).all())
        for i in items:
            i.unread = i.key not in read
    return items


async def _remember(db: AsyncSession, user_id: int, key: str) -> None:
    """Отметить прочитанным. Повторная отметка (двойной клик, два запроса сразу) — не ошибка."""
    try:
        async with db.begin_nested():
            db.add(NotificationRead(user_id=user_id, key=key, read_at=utcnow()))
    except IntegrityError:
        pass


async def mark_read(db: AsyncSession, user_id: int, kind: str, ref: int) -> None:
    if kind in KINDS:
        await _remember(db, user_id, f"{kind}:{ref}")
        await db.commit()


async def mark_all_read(db: AsyncSession, user: User, student: Student | None) -> None:
    """Все непрочитанные за горизонт, а не только те, что влезают в ленту из LIMIT."""
    for i in await feed(db, user, student, limit=ALL_LIMIT):
        if i.unread:
            await _remember(db, user.id, i.key)
    await db.commit()


async def teacher_groups(db: AsyncSession, teacher_id: int) -> list[StudyGroup]:
    """Группы, у которых этот преподаватель ведёт курс, — только им он может писать."""
    return list((await db.scalars(
        select(StudyGroup).join(Course, Course.group_id == StudyGroup.id)
        .where(Course.teacher_id == teacher_id).distinct().order_by(StudyGroup.number)
    )).all())


async def faculty_names(db: AsyncSession) -> list[str]:
    return list((await db.scalars(
        select(Student.faculty_name).where(Student.faculty_name != "").distinct().order_by(Student.faculty_name)
    )).all())


async def programs(db: AsyncSession) -> list[str]:
    """Программы для адресации потока: как в `students.speciality`."""
    return list((await db.scalars(
        select(Student.speciality).where(Student.speciality != "").distinct().order_by(Student.speciality)
    )).all())


async def search_students(db: AsyncSession, raw: str, limit: int = 8) -> list[tuple[Student, str]]:
    """Поиск адресата по ФИО (только для сотрудника деканата). Регистр: SQLite не понижает кириллицу,
    поэтому перебираем типичные написания, а не `lower()`."""
    q = " ".join(raw.split())
    if len(q) < 2:
        return []
    forms = {q, q.lower(), q.upper(), q.capitalize(), q.title()}
    rows = (await db.execute(
        select(Student, StudyGroup.number)
        .outerjoin(StudyGroup, StudyGroup.id == Student.study_group_id)
        .where(or_(*[Student.full_name.contains(f) for f in forms]))
        .order_by(Student.full_name).limit(limit)
    )).all()
    return [(s, num or "—") for s, num in rows]


async def find_group(db: AsyncSession, raw: str) -> StudyGroup | None:
    raw = raw.strip()
    if not raw:
        return None
    return (await db.scalars(
        select(StudyGroup).where(or_(StudyGroup.number == raw, StudyGroup.name == raw)).limit(1)
    )).first()


class AnnouncementError(ValueError):
    """Текст ошибки показывается автору в форме."""


async def create(db: AsyncSession, *, user: User, source: str, title: str, body: str,
                 audience: str, faculty_name: str | None = None, group: StudyGroup | None = None,
                 program: str | None = None, course: int | None = None,
                 student: Student | None = None) -> Announcement:
    title, body = title.strip(), body.strip()
    if not title:
        raise AnnouncementError("Укажите заголовок.")
    if len(title) > TITLE_MAX or len(body) > BODY_MAX:
        raise AnnouncementError(f"Заголовок — до {TITLE_MAX} знаков, текст — до {BODY_MAX}.")
    if source not in Source.TITLES or (source == Source.TEACHER) != (user.role == Role.TEACHER):
        raise AnnouncementError("Недопустимый отправитель.")
    if audience not in (Audience.ALL, Audience.FACULTY, Audience.GROUP, Audience.STREAM, Audience.STUDENT):
        raise AnnouncementError("Недопустимая аудитория.")
    if audience == Audience.FACULTY and not faculty_name:
        raise AnnouncementError("Выберите факультет.")
    if source == Source.TEACHER:
        # Инвариант домена: преподаватель пишет только группам, у которых ведёт курс.
        own_ids = {g.id for g in await teacher_groups(db, user.teacher_id)}
        if audience != Audience.GROUP or group is None or group.id not in own_ids:
            raise AnnouncementError("Выберите одну из своих групп.")
    if audience == Audience.GROUP and group is None:
        raise AnnouncementError("Такой группы нет. Укажите номер как «1МО-01».")
    if audience == Audience.STREAM and (not program or course is None or not 1 <= course <= 6):
        raise AnnouncementError("Для потока укажите программу и курс.")
    if audience == Audience.STUDENT and student is None:
        raise AnnouncementError("Выберите студента из результатов поиска.")
    row = Announcement(
        source=source, author_user_id=user.id, title=title, body=body, audience=audience,
        faculty_name=faculty_name if audience == Audience.FACULTY else None,
        group_id=group.id if audience == Audience.GROUP and group else None,
        program=program if audience == Audience.STREAM else None,
        course=course if audience == Audience.STREAM else None,
        student_id=student.id if audience == Audience.STUDENT and student else None, created_at=utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


async def own(db: AsyncSession, user_id: int, limit: int = 20) -> list[Announcement]:
    return list((await db.scalars(
        select(Announcement).where(Announcement.author_user_id == user_id)
        .order_by(Announcement.created_at.desc()).limit(limit)
    )).all())
