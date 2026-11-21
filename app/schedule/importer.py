"""Импорт расписания из РУЗ в БД с журналом изменений.

Порядок одного запуска:

1. Справочники факультетов и групп — целиком, одним запросом каждый.
2. Расписание каждой группы за период — по одному запросу на группу, последовательно,
   с паузой (её выдерживает RuzClient). Транзакция — на группу: упавший посередине
   импорт не теряет уже загруженное.
3. Итог по периоду: отмены, переносы, новые занятия.

Правила журнала изменений:

* **Исчезнувшее занятие — отмена, а не удаление.** Строка остаётся со статусом
  `cancelled`, чтобы кабинет показал «отменено», а не молча убрал пару.
* **Отмену фиксируем, только если все группы занятия загрузились в этом запуске.**
  Упала группа — про её занятия мы ничего нового не знаем, и молчать честнее.
* **Пустой ответ по группе, у которой в БД есть занятия за период, — сбой, а не
  отмена всего.** Такая группа считается незагруженной.
* **Сменились дата или время у той же записи РУЗ — перенос.** Отмена плюс новое занятие
  той же дисциплины, вида и тех же групп в пределах `MOVE_WINDOW` — тоже перенос:
  для студента это одно событие.
* **«Добавлено» пишем только для дат, которые уже покрывал прошлый успешный импорт.**
  Иначе первый импорт засыпал бы журнал сотнями тысяч «новых» пар.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache import get_cache
from app.cache.invalidation import SCHEDULE
from app.models import (
    Faculty,
    GroupScheduleCoverage,
    ImportRun,
    Lesson,
    LessonChange,
    LessonGroup,
    StudyGroup,
    Teacher,
    lesson_teachers,
)
from app.models.schedule import ChangeKind, ImportStatus, LessonStatus
from app.schedule.fake_names import allowed_surnames, fake_teacher_name, feminine_surname
from app.schedule.normalize import (
    GroupData,
    LessonData,
    faculty_from_raw,
    group_from_raw,
    lesson_from_raw,
    real_surnames,
)
from app.schedule.ruz_client import RuzClient, RuzError

log = logging.getLogger(__name__)

MOVE_WINDOW = timedelta(days=14)
_CHUNK = 500
_TRACKED_FIELDS = ("date", "begins_at", "ends_at", "discipline", "kind_of_work", "auditorium", "building")
_MOVE_FIELDS = {"date", "begins_at", "ends_at"}


def semester_bounds(today: date) -> tuple[date, date]:
    """Осенний семестр — с 1 сентября по 31 января (с сессией), весенний — с 1 февраля по 30 июня."""
    if today.month >= 8:
        return date(today.year, 9, 1), date(today.year + 1, 1, 31)
    if today.month == 1:
        return date(today.year - 1, 9, 1), date(today.year, 1, 31)
    return date(today.year, 2, 1), date(today.year, 6, 30)


@dataclass
class ImportStats:
    run_id: int = 0
    status: str = ImportStatus.RUNNING
    groups_total: int = 0
    groups_ok: int = 0
    groups_populated: int = 0
    groups_failed: int = 0
    lessons_seen: int = 0
    changes: dict[str, int] = field(default_factory=dict)
    failed_groups: list[str] = field(default_factory=list)


GroupFilter = Callable[[GroupData], bool]


def _chunks(items: Sequence[int]) -> Iterable[Sequence[int]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _snapshot(lesson: Lesson) -> dict[str, str | None]:
    return {name: _jsonable(getattr(lesson, name)) for name in _TRACKED_FIELDS}


def _jsonable(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # type: ignore[no-any-return]
    return str(value)


class ScheduleImporter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client: RuzClient,
        *,
        fake_name_salt: str,
        concurrency: int = 1,
        report_additions: bool = True,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._sf = session_factory
        self._client = client
        self._salt = fake_name_salt
        self._concurrency = max(1, concurrency)
        self._report_additions = report_additions
        self._now = now

        self._run_id = 0
        self._teacher_ids: dict[int, int] = {}
        self._teacher_names: dict[int, str] = {}
        self._taken_names: set[str] = set()
        self._surnames: tuple[str, ...] = ()
        self._new_lesson_ids: list[int] = []
        self._ok_groups: set[int] = set()
        self._known_groups: set[int] = set()
        """Группы, у которых занятия были в БД до этого запуска: только у них новое занятие — «добавлено»."""
        self._changes: dict[str, int] = {}

    async def run(
        self,
        start: date,
        finish: date,
        *,
        group_filter: GroupFilter | None = None,
        limit: int | None = None,
        nonempty_limit: int | None = None,
    ) -> ImportStats:
        """nonempty_limit ограничивает число групп с занятиями, не число попыток."""
        if nonempty_limit is not None and nonempty_limit <= 0:
            raise ValueError("nonempty_limit должен быть положительным")
        stats = ImportStats()
        async with self._sf() as session:
            run = ImportRun(started_at=self._now(), period_start=start, period_end=finish)
            session.add(run)
            await session.commit()
            self._run_id = stats.run_id = run.id

        try:
            groups = await self._import_dictionaries()
            selected = [g for g in groups if g.has_schedule and (group_filter is None or group_filter(g))]
            if limit is not None:
                selected = selected[:limit]
            stats.groups_total = len(selected) if nonempty_limit is None else 0
            self._surnames = allowed_surnames(real_surnames(await self._client.lecturers()))
            await self._load_teachers()

            # Запросы к РУЗ — до `concurrency` одновременно, запись в БД — строго по одной группе.
            fetch_slots = asyncio.Semaphore(self._concurrency)
            db_lock = asyncio.Lock()
            done = 0

            async def handle(group: GroupData) -> None:
                nonlocal done
                try:
                    async with fetch_slots:
                        raw = await self._client.group_schedule(group.id, start, finish)
                    async with db_lock:
                        seen = await self._import_group(group.id, raw, start, finish)
                except (RuzError, KeyError, ValueError) as exc:
                    stats.groups_failed += 1
                    stats.failed_groups.append(group.number)
                    log.warning("группа %s (%s): %s", group.number, group.id, exc)
                else:
                    self._ok_groups.add(group.id)
                    stats.groups_ok += 1
                    stats.groups_populated += int(seen > 0)
                    stats.lessons_seen += seen
                done += 1
                if done % 50 == 0 or done == len(selected):
                    log.info("загружено групп: %s из %s", done, len(selected))

            if nonempty_limit is None:
                await asyncio.gather(*(handle(g) for g in selected))
            else:
                # Пустые ответы и ошибки не расходуют цель. Не запускаем лишние запросы,
                # когда осталось набрать меньше групп, чем разрешено параллельных запросов.
                offset = 0
                while offset < len(selected) and stats.groups_populated < nonempty_limit:
                    size = min(self._concurrency, nonempty_limit - stats.groups_populated)
                    batch = selected[offset:offset + size]
                    stats.groups_total += len(batch)
                    await asyncio.gather(*(handle(g) for g in batch))
                    offset += len(batch)
            await self._finalize(start, finish)
        except BaseException as exc:
            await self._finish_run(stats, ImportStatus.FAILED, error=repr(exc))
            raise

        status = ImportStatus.OK if stats.groups_failed == 0 else ImportStatus.PARTIAL
        if stats.groups_total and stats.groups_ok == 0:
            status = ImportStatus.FAILED
        await self._finish_run(stats, status)
        return stats

    # --- справочники ---------------------------------------------------------

    async def _import_dictionaries(self) -> list[GroupData]:
        faculties = [faculty_from_raw(r) for r in await self._client.faculties()]
        groups = [group_from_raw(r) for r in await self._client.groups()]
        known_faculties = {f.id for f in faculties}

        async with self._sf() as session:
            for f in faculties:
                await session.merge(Faculty(id=f.id, abbr=f.abbr, name=f.name))
            for g in groups:
                await session.merge(
                    StudyGroup(
                        id=g.id,
                        name=g.name,
                        number=g.number,
                        faculty_id=g.faculty_id if g.faculty_id in known_faculties else None,
                        course=g.course,
                        form_of_education=g.form_of_education,
                        speciality=g.speciality,
                        specialization=g.specialization,
                    )
                )
            await session.commit()
        return groups

    async def _load_teachers(self) -> None:
        """Загрузить известных преподавателей и переименовать тех, чья вымышленная фамилия
        с прошлого импорта совпала с фамилией реального преподавателя (список меняется)."""
        allowed = {form for s in self._surnames for form in (s, feminine_surname(s))}
        async with self._sf() as session:
            teachers = (await session.scalars(select(Teacher))).all()
            self._taken_names = {t.full_name for t in teachers}
            for teacher in teachers:
                if teacher.full_name.split()[0] not in allowed:
                    self._taken_names.discard(teacher.full_name)
                    teacher.full_name = fake_teacher_name(
                        teacher.ruz_oid, self._salt, self._taken_names, self._surnames
                    )
                    self._taken_names.add(teacher.full_name)
                self._teacher_ids[teacher.ruz_oid] = teacher.id
                self._teacher_names[teacher.id] = teacher.full_name
            await session.commit()

    async def _ensure_teachers(self, oids: set[int]) -> None:
        """Завести преподавателей отдельной транзакцией — до транзакции группы.

        Так откат упавшей группы не оставляет в кэше id преподавателей, которых нет в БД.
        """
        missing = sorted(oids - self._teacher_ids.keys())
        if not missing:
            return
        created: list[Teacher] = []
        async with self._sf() as session:
            taken = set(self._taken_names)
            for oid in missing:
                name = fake_teacher_name(oid, self._salt, taken, self._surnames)
                taken.add(name)
                teacher = Teacher(ruz_oid=oid, full_name=name)
                session.add(teacher)
                created.append(teacher)
            await session.commit()
        for teacher in created:
            self._teacher_ids[teacher.ruz_oid] = teacher.id
            self._teacher_names[teacher.id] = teacher.full_name
            self._taken_names.add(teacher.full_name)

    # --- одна группа ---------------------------------------------------------

    async def _import_group(self, group_id: int, raw: list[dict], start: date, finish: date) -> int:
        lessons: dict[int, LessonData] = {}
        for item in raw:
            data = lesson_from_raw(item)
            if start <= data.date <= finish:
                lessons[data.id] = data
        await self._ensure_teachers({oid for data in lessons.values() for oid in data.lecturer_oids})

        # Если транзакция группы откатится, откатываем и состояние в памяти.
        changes_before = dict(self._changes)
        new_before = len(self._new_lesson_ids)
        try:
            return await self._store_group(group_id, lessons, start, finish)
        except BaseException:
            self._changes = changes_before
            del self._new_lesson_ids[new_before:]
            raise

    async def _store_group(
        self, group_id: int, lessons: dict[int, LessonData], start: date, finish: date
    ) -> int:
        async with self._sf() as session:
            had_lessons = await session.scalar(
                select(LessonGroup.lesson_id)
                .where(LessonGroup.group_id == group_id, LessonGroup.last_seen_run_id != self._run_id)
                .limit(1)
            )
            if had_lessons is not None:
                self._known_groups.add(group_id)
            if not lessons:
                known = await session.scalar(
                    select(func.count())
                    .select_from(LessonGroup)
                    .join(Lesson, Lesson.id == LessonGroup.lesson_id)
                    .where(
                        LessonGroup.group_id == group_id,
                        Lesson.status == LessonStatus.ACTIVE,
                        Lesson.date.between(start, finish),
                    )
                )
                if known:
                    raise RuzError(f"пустой ответ, хотя в БД {known} занятий за период")
                return 0

            ids = list(lessons)
            existing: dict[int, Lesson] = {}
            linked: set[int] = set()
            teachers_by_lesson: dict[int, set[int]] = {}
            for chunk in _chunks(ids):
                for row in (await session.scalars(select(Lesson).where(Lesson.id.in_(chunk)))).all():
                    existing[row.id] = row
                linked.update(
                    (
                        await session.scalars(
                            select(LessonGroup.lesson_id).where(
                                LessonGroup.group_id == group_id, LessonGroup.lesson_id.in_(chunk)
                            )
                        )
                    ).all()
                )
                for lesson_id, teacher_id in (
                    await session.execute(
                        select(lesson_teachers.c.lesson_id, lesson_teachers.c.teacher_id).where(
                            lesson_teachers.c.lesson_id.in_(chunk)
                        )
                    )
                ).all():
                    teachers_by_lesson.setdefault(lesson_id, set()).add(teacher_id)

            now = self._now()
            for data in lessons.values():
                teacher_ids = {self._teacher_ids[oid] for oid in data.lecturer_oids}
                row = existing.get(data.id)
                if row is None:
                    row = Lesson(
                        id=data.id,
                        first_seen_at=now,
                        last_seen_at=now,
                        last_seen_run_id=self._run_id,
                        status=LessonStatus.ACTIVE,
                    )
                    self._apply(row, data)
                    session.add(row)
                    await session.flush()
                    await self._set_teachers(session, data.id, set(), teacher_ids)
                    self._new_lesson_ids.append(data.id)
                elif row.last_seen_run_id != self._run_id:
                    old_teachers = teachers_by_lesson.get(data.id, set())
                    self._update_existing(session, row, data, old_teachers, teacher_ids)
                    await self._set_teachers(session, data.id, old_teachers, teacher_ids)
                row.last_seen_at = now
                row.last_seen_run_id = self._run_id

                if data.id in linked:
                    await session.execute(
                        update(LessonGroup)
                        .where(LessonGroup.lesson_id == data.id, LessonGroup.group_id == group_id)
                        .values(last_seen_run_id=self._run_id)
                    )
                else:
                    session.add(
                        LessonGroup(lesson_id=data.id, group_id=group_id, last_seen_run_id=self._run_id)
                    )
            semester_start, semester_end = semester_bounds(start)
            if start <= semester_start and finish >= semester_end:
                # В той же транзакции, что занятия. Короткий импорт не создаёт и не стирает
                # подтверждение полного семестра; старые импорты не восстанавливаем наугад.
                await session.merge(GroupScheduleCoverage(
                    group_id=group_id, period_start=semester_start,
                    period_end=semester_end, run_id=self._run_id,
                ))
            await session.commit()
        return len(lessons)

    @staticmethod
    def _apply(row: Lesson, data: LessonData) -> None:
        row.date = data.date
        row.begins_at = data.begins_at
        row.ends_at = data.ends_at
        row.lesson_number = data.lesson_number
        row.discipline = data.discipline
        row.kind_of_work = data.kind_of_work
        row.auditorium = data.auditorium
        row.building = data.building
        row.stream = data.stream
        row.subgroup = data.subgroup
        row.ruz_modified_at = data.ruz_modified_at

    def _update_existing(
        self,
        session: AsyncSession,
        row: Lesson,
        data: LessonData,
        old_teachers: set[int],
        new_teachers: set[int],
    ) -> None:
        diff: dict[str, list[object]] = {}
        for name in _TRACKED_FIELDS:
            old, new = getattr(row, name), getattr(data, name)
            if old != new:
                diff[name] = [_jsonable(old), _jsonable(new)]
        if old_teachers != new_teachers:
            diff["teachers"] = [self._names(old_teachers), self._names(new_teachers)]

        was_cancelled = row.status == LessonStatus.CANCELLED
        self._apply(row, data)
        if was_cancelled:
            row.status = LessonStatus.ACTIVE
            row.moved_to_id = None
            self._log(session, ChangeKind.RESTORED, row.id, {"changed": diff} if diff else {})
        elif diff:
            kind = ChangeKind.MOVED if _MOVE_FIELDS & diff.keys() else ChangeKind.CHANGED
            self._log(session, kind, row.id, {"changed": diff})

    def _names(self, teacher_ids: set[int]) -> list[str]:
        return sorted(self._teacher_names[t] for t in teacher_ids)

    @staticmethod
    async def _set_teachers(session: AsyncSession, lesson_id: int, old: set[int], new: set[int]) -> None:
        if gone := old - new:
            await session.execute(
                delete(lesson_teachers).where(
                    lesson_teachers.c.lesson_id == lesson_id, lesson_teachers.c.teacher_id.in_(gone)
                )
            )
        if added := new - old:
            await session.execute(
                insert(lesson_teachers), [{"lesson_id": lesson_id, "teacher_id": t} for t in added]
            )

    def _log(
        self,
        session: AsyncSession,
        kind: str,
        lesson_id: int,
        details: dict,
        previous_lesson_id: int | None = None,
    ) -> None:
        session.add(
            LessonChange(
                run_id=self._run_id,
                lesson_id=lesson_id,
                previous_lesson_id=previous_lesson_id,
                kind=kind,
                details=details,
                detected_at=self._now(),
            )
        )
        self._changes[kind] = self._changes.get(kind, 0) + 1

    # --- итог по периоду -----------------------------------------------------

    async def _finalize(self, start: date, finish: date) -> None:
        async with self._sf() as session:
            if self._ok_groups:
                await self._drop_stale_links(session)
            cancelled = await self._find_cancelled(session, start, finish)
            added = await self._find_added(session, start, finish)
            groups_of = await self._groups_of(session, [c.id for c in cancelled] + [a.id for a in added])

            for new in sorted(added, key=lambda lesson: (lesson.date, lesson.begins_at)):
                old = self._match_move(new, cancelled, groups_of)
                if old is not None:
                    cancelled.remove(old)
                    old.status = LessonStatus.CANCELLED
                    old.moved_to_id = new.id
                    self._log(
                        session,
                        ChangeKind.MOVED,
                        new.id,
                        {"from": _snapshot(old), "to": _snapshot(new)},
                        previous_lesson_id=old.id,
                    )
                else:
                    self._log(session, ChangeKind.ADDED, new.id, {"lesson": _snapshot(new)})

            for old in cancelled:
                old.status = LessonStatus.CANCELLED
                self._log(session, ChangeKind.CANCELLED, old.id, {"lesson": _snapshot(old)})
            await session.commit()

    async def _drop_stale_links(self, session: AsyncSession) -> None:
        """Занятие есть в РУЗ, но у загруженной группы его больше нет — убрать связь с группой."""
        seen_this_run = select(Lesson.id).where(Lesson.last_seen_run_id == self._run_id)
        for chunk in _chunks(sorted(self._ok_groups)):
            await session.execute(
                delete(LessonGroup).where(
                    LessonGroup.group_id.in_(chunk),
                    LessonGroup.last_seen_run_id != self._run_id,
                    LessonGroup.lesson_id.in_(seen_this_run),
                )
            )

    async def _find_cancelled(self, session: AsyncSession, start: date, finish: date) -> list[Lesson]:
        candidates = (
            await session.scalars(
                select(Lesson).where(
                    Lesson.status == LessonStatus.ACTIVE,
                    Lesson.date.between(start, finish),
                    (Lesson.last_seen_run_id.is_(None)) | (Lesson.last_seen_run_id != self._run_id),
                )
            )
        ).all()
        if not candidates:
            return []
        groups_of = await self._groups_of(session, [c.id for c in candidates])
        return [c for c in candidates if groups_of.get(c.id, set()) <= self._ok_groups]

    async def _find_added(self, session: AsyncSession, start: date, finish: date) -> list[Lesson]:
        if not self._report_additions or not self._new_lesson_ids:
            return []
        covered = (
            await session.execute(
                select(ImportRun.period_start, ImportRun.period_end).where(
                    ImportRun.id != self._run_id,
                    ImportRun.status.in_([ImportStatus.OK, ImportStatus.PARTIAL]),
                    and_(ImportRun.period_start <= finish, ImportRun.period_end >= start),
                )
            )
        ).all()
        if not covered:
            return []
        # Дата покрыта прошлым импортом — мало: прошлый импорт мог быть по другим группам.
        # Нужно, чтобы хотя бы одна группа занятия уже загружалась раньше.
        groups_of = await self._groups_of(session, self._new_lesson_ids)
        result: list[Lesson] = []
        for chunk in _chunks(self._new_lesson_ids):
            for row in (await session.scalars(select(Lesson).where(Lesson.id.in_(chunk)))).all():
                if groups_of.get(row.id, set()) & self._known_groups and any(
                    ps <= row.date <= pe for ps, pe in covered
                ):
                    result.append(row)
        return result

    @staticmethod
    async def _groups_of(session: AsyncSession, lesson_ids: list[int]) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        for chunk in _chunks(lesson_ids):
            for lesson_id, group_id in (
                await session.execute(
                    select(LessonGroup.lesson_id, LessonGroup.group_id).where(
                        LessonGroup.lesson_id.in_(chunk)
                    )
                )
            ).all():
                result.setdefault(lesson_id, set()).add(group_id)
        return result

    @staticmethod
    def _match_move(new: Lesson, cancelled: list[Lesson], groups_of: dict[int, set[int]]) -> Lesson | None:
        new_groups = groups_of.get(new.id, set())
        options = [
            old
            for old in cancelled
            if old.discipline == new.discipline
            and old.kind_of_work == new.kind_of_work
            and groups_of.get(old.id, set()) == new_groups
            and abs(old.date - new.date) <= MOVE_WINDOW
        ]
        if not options:
            return None
        return min(options, key=lambda old: abs(old.date - new.date))

    async def _finish_run(self, stats: ImportStats, status: str, error: str | None = None) -> None:
        stats.status = status
        stats.changes = dict(self._changes)
        async with self._sf() as session:
            await session.execute(
                update(ImportRun)
                .where(ImportRun.id == self._run_id)
                .values(
                    finished_at=self._now(),
                    status=status,
                    groups_total=stats.groups_total,
                    groups_ok=stats.groups_ok,
                    groups_failed=stats.groups_failed,
                    lessons_seen=stats.lessons_seen,
                    error=error,
                )
            )
            await session.commit()
        # Явно, хотя слушатель сессии (app/cache/invalidation.py) уже сбросил поколение после
        # коммита каждой группы: итог импорта — точка, после которой кэш расписания точно свежий.
        await get_cache().bump(SCHEDULE)
