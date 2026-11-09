"""Лента колокольчика: объявления + статусы справок + новые баллы и ДЗ.

Ничего не копируется в отдельную таблицу уведомлений: события справок и баллов читаются из
их журналов в момент открытия ленты, объявления — из `announcements`. Всё фильтруется по
пользователю из сессии (чужое увидеть нельзя), прочитанное — `notification_reads`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import utcnow
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
KINDS = {"ann", "cert", "grade"}
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


async def _announcements(db: AsyncSession, student: Student | None, since: datetime, limit: int) -> list[Item]:
    q = select(Announcement).where(Announcement.created_at >= since)
    if student is not None:
        cond = [Announcement.audience == Audience.ALL]
        if student.faculty_name:
            cond.append(and_(Announcement.audience == Audience.FACULTY,
                             Announcement.faculty_name == student.faculty_name))
        if student.study_group_id is not None:
            cond.append(and_(Announcement.audience == Audience.GROUP,
                             Announcement.group_id == student.study_group_id))
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


async def _grades(db: AsyncSession, student_id: int, since: datetime, limit: int) -> list[Item]:
    rows = (await db.execute(
        select(GradeEvent, Discipline.name)
        .join(Course, Course.id == GradeEvent.course_id)
        .join(Discipline, Discipline.id == Course.discipline_id)
        .where(GradeEvent.student_id == student_id, GradeEvent.created_at >= since)
        .order_by(GradeEvent.created_at.desc()).limit(limit)
    )).all()
    return [Item("grade", ev.id, Source.TEACHER, f"{name}: {ev.text}", "", ev.created_at, "/grades")
            for ev, name in rows]


async def feed(db: AsyncSession, user: User, student: Student | None, *, now: datetime | None = None,
               limit: int = LIMIT) -> list[Item]:
    """Свежие уведомления пользователя, новые сверху, с отметкой прочитанного."""
    since = (now or utcnow()) - HORIZON
    items = await _announcements(db, student, since, limit)
    if student is not None:
        items += await _certificates(db, student.id, since, limit)
        items += await _grades(db, student.id, since, limit)
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
                 audience: str, faculty_name: str | None = None, group: StudyGroup | None = None) -> Announcement:
    title, body = title.strip(), body.strip()
    if not title:
        raise AnnouncementError("Укажите заголовок.")
    if len(title) > TITLE_MAX or len(body) > BODY_MAX:
        raise AnnouncementError(f"Заголовок — до {TITLE_MAX} знаков, текст — до {BODY_MAX}.")
    if source not in Source.TITLES or (source == Source.TEACHER) != (user.role == Role.TEACHER):
        raise AnnouncementError("Недопустимый отправитель.")
    if audience not in (Audience.ALL, Audience.FACULTY, Audience.GROUP):
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
    row = Announcement(
        source=source, author_user_id=user.id, title=title, body=body, audience=audience,
        faculty_name=faculty_name if audience == Audience.FACULTY else None,
        group_id=group.id if audience == Audience.GROUP and group else None, created_at=utcnow(),
    )
    db.add(row)
    await db.commit()
    return row


async def own(db: AsyncSession, user_id: int, limit: int = 20) -> list[Announcement]:
    return list((await db.scalars(
        select(Announcement).where(Announcement.author_user_id == user_id)
        .order_by(Announcement.created_at.desc()).limit(limit)
    )).all())
