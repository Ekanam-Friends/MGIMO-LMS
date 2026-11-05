"""Команды и кнопки бота. Набор — бесплатная часть ikanam_schedule_bot, перенесённая на МГИМО.

    /start <код>  — подключение по ссылке из профиля кабинета (пароль в бот не вводится)
    /today /tomorrow /week — расписание текстом, листается кнопками
    /exams        — контрольные точки (зачёты, экзамены, контрольные) из расписания
    /calendar     — ссылка-подписка и файл .ics, пошаговые инструкции по платформам
    /certificates — статусы заявок на справки
    /settings     — утренняя сводка, уведомления, напоминания, перевыпуск ссылки
    /status       — когда расписание обновлялось
    /logout       — отключить бота и удалить всё, что он хранит

Студент определяется только по telegram id отправителя (`link.by_telegram`). Никакой
параметр кнопки или текста не выбирает, чьё расписание показать: в callback_data —
только даты и значения настроек, и те проверяются по белому списку.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import ics
from app.bot import link as links
from app.bot.formatting import format_date, format_day, format_exams, format_week, is_exam
from app.certificates import service as certs
from app.core.config import Settings
from app.models import ImportRun, Student, TelegramLink
from app.models.certificates import CertificateKind
from app.models.schedule import ImportStatus
from app.web.schedule_view import lessons_for, week_start

log = logging.getLogger(__name__)
router = Router(name="lms")

MOSCOW = ZoneInfo("Europe/Moscow")
EXAMS_AHEAD = 60

DIGEST_OPTIONS: tuple[str | None, ...] = ("07:00", "07:30", "08:00", "08:30", None)
REMINDER_OPTIONS: tuple[int | None, ...] = (None, 10, 15, 30)
EXAM_OPTIONS: tuple[int | None, ...] = (None, 1, 3, 7)

BTN_TODAY, BTN_TOMORROW, BTN_WEEK = "📅 Сегодня", "📅 Завтра", "🗓 Неделя"
BTN_EXAMS, BTN_CAL, BTN_CERTS = "🎯 Контрольные", "📆 В календарь", "📄 Справки"
BTN_SETTINGS, BTN_HELP = "⚙️ Настройки", "❓ Помощь"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_TOMORROW)],
        [KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_EXAMS)],
        [KeyboardButton(text=BTN_CAL), KeyboardButton(text=BTN_CERTS)],
        [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

DISCLAIMER = "<i>Концепт для конкурса MGIMO×AI Challenge, не официальный сервис МГИМО. Все данные студентов вымышленные.</i>"


def today_msk() -> date:
    return datetime.now(MOSCOW).date()


def help_text(settings: Settings) -> str:
    return (
        "Я показываю расписание из личного кабинета студента МГИМО и кладу его в календарь телефона.\n\n"
        "<b>Что умею</b>\n"
        "/today, /tomorrow, /week — занятия: время, аудитория, преподаватель\n"
        "/exams — ближайшие зачёты, экзамены и контрольные\n"
        "/calendar — подписка календаря (обновляется сама) или файл .ics\n"
        "/certificates — статусы заявок на справки\n"
        "/settings — утренняя сводка, уведомления, напоминания\n"
        "/status — когда расписание обновлялось\n"
        "/logout — отключить бота и удалить данные\n\n"
        "Сам пишу, когда занятие переносят, отменяют или меняют аудиторию, когда меняется статус "
        "справки и за несколько дней до зачёта.\n\n"
        "<b>Что нужно знать</b>\n"
        "• Пароль от кабинета боту не нужен: подключение — по одноразовой ссылке из профиля.\n"
        "• Ссылка на календарь личная. Если утекла — перевыпустите её в /settings.\n"
        "• /logout удаляет привязку, ссылку на календарь и историю напоминаний.\n\n"
        + DISCLAIMER
    )


def not_linked_text(settings: Settings) -> str:
    return (
        "Бот ещё не подключён к кабинету.\n\n"
        f"Откройте <a href=\"{escape(settings.public_base_url.rstrip('/'))}/profile\">профиль в кабинете</a> "
        "и нажмите «Подключить Telegram» — я узнаю вас без пароля."
    )


async def _me(sf: async_sessionmaker[AsyncSession], telegram_id: int) -> tuple[TelegramLink, Student] | None:
    async with sf() as session:
        return await links.by_telegram(session, telegram_id)


async def _need_me(message: Message, sf, settings: Settings):
    me = await _me(sf, message.from_user.id)
    if me is None:
        await message.answer(not_linked_text(settings), reply_markup=MAIN_KB)
    return me


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc):
            await query.message.answer(text, reply_markup=markup)
    await query.answer()


# --- старт и привязка ---------------------------------------------------------


@router.message(CommandStart())
async def on_start(message: Message, command: CommandObject, sf, settings: Settings) -> None:
    code = (command.args or "").strip()
    if code:
        async with sf() as session:
            try:
                _, student = await links.consume_code(session, code, message.from_user.id)
            except links.LinkError as exc:
                await session.rollback()
                await message.answer(str(exc), reply_markup=MAIN_KB)
                return
            await session.commit()
        first = student.full_name.split()[1] if len(student.full_name.split()) > 1 else student.full_name
        await message.answer(
            f"Готово, {escape(first)}! Кабинет подключён.\n\n"
            "Утренняя сводка придёт в 07:30, о переносах и справках напишу сам — поменять можно в /settings.",
            reply_markup=MAIN_KB,
        )
        await send_day(message, sf, settings, today_msk())
        return
    me = await _me(sf, message.from_user.id)
    await message.answer(help_text(settings), reply_markup=MAIN_KB)
    if me is None:
        await message.answer(not_linked_text(settings))


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def on_help(message: Message, settings: Settings) -> None:
    await message.answer(help_text(settings), reply_markup=MAIN_KB)


# --- расписание ---------------------------------------------------------------


def _day_kb(day: date) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="◀", callback_data=f"day:{(day - timedelta(days=1)).isoformat()}")
    b.button(text="Сегодня", callback_data=f"day:{today_msk().isoformat()}")
    b.button(text="▶", callback_data=f"day:{(day + timedelta(days=1)).isoformat()}")
    b.button(text="🗓 Неделя", callback_data=f"week:{week_start(day).isoformat()}")
    b.adjust(3, 1)
    return b.as_markup()


def _week_kb(start: date) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="◀ неделя", callback_data=f"week:{(start - timedelta(days=7)).isoformat()}")
    b.button(text="Текущая", callback_data=f"week:{week_start(today_msk()).isoformat()}")
    b.button(text="неделя ▶", callback_data=f"week:{(start + timedelta(days=7)).isoformat()}")
    b.adjust(3)
    return b.as_markup()


def _title(day: date) -> str:
    t = today_msk()
    prefix = {t: "Сегодня", t + timedelta(days=1): "Завтра"}.get(day)
    return f"{prefix}, {format_date(day)}" if prefix else format_date(day).capitalize()


async def day_text(sf, student: Student, day: date) -> str:
    async with sf() as session:
        days = await lessons_for(session, student.study_group_id, day, day)
    if student.study_group_id is None:
        return "В кабинете у вас не указана группа — расписания нет."
    return format_day(day, days.get(day, []), title=_title(day))


async def send_day(message: Message, sf, settings: Settings, day: date) -> None:
    me = await _need_me(message, sf, settings)
    if me is None:
        return
    await message.answer(await day_text(sf, me[1], day), reply_markup=_day_kb(day))


async def week_text(sf, student: Student, start: date) -> str:
    async with sf() as session:
        days = await lessons_for(session, student.study_group_id, start, start + timedelta(days=6))
    return format_week(start, days)


@router.message(Command("today"))
@router.message(F.text == BTN_TODAY)
async def on_today(message: Message, sf, settings: Settings) -> None:
    await send_day(message, sf, settings, today_msk())


@router.message(Command("tomorrow"))
@router.message(F.text == BTN_TOMORROW)
async def on_tomorrow(message: Message, sf, settings: Settings) -> None:
    await send_day(message, sf, settings, today_msk() + timedelta(days=1))


@router.message(Command("week"))
@router.message(F.text == BTN_WEEK)
async def on_week(message: Message, sf, settings: Settings) -> None:
    me = await _need_me(message, sf, settings)
    if me is None:
        return
    start = week_start(today_msk())
    await message.answer(await week_text(sf, me[1], start), reply_markup=_week_kb(start))


def _parse_day(raw: str) -> date | None:
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    # Листать дальше года смысла нет, а дата из callback_data — от клиента.
    return day if abs((day - today_msk()).days) <= 366 else None


@router.callback_query(F.data.startswith("day:"))
async def on_day_nav(query: CallbackQuery, sf, settings: Settings) -> None:
    me = await _me(sf, query.from_user.id)
    day = _parse_day(query.data[4:])
    if me is None or day is None:
        await query.answer("Подключите бота из профиля кабинета", show_alert=me is None)
        return
    await _edit(query, await day_text(sf, me[1], day), _day_kb(day))


@router.callback_query(F.data.startswith("week:"))
async def on_week_nav(query: CallbackQuery, sf, settings: Settings) -> None:
    me = await _me(sf, query.from_user.id)
    day = _parse_day(query.data[5:])
    if me is None or day is None:
        await query.answer("Подключите бота из профиля кабинета", show_alert=me is None)
        return
    start = week_start(day)
    await _edit(query, await week_text(sf, me[1], start), _week_kb(start))


# --- контрольные точки --------------------------------------------------------


async def upcoming_exams(session: AsyncSession, group_id: int | None, start: date, days: int = EXAMS_AHEAD):
    found = await lessons_for(session, group_id, start, start + timedelta(days=days))
    return [(d, slot, lesson) for d in sorted(found) for slot in found[d] for lesson in slot.lessons if is_exam(lesson)]


@router.message(Command("exams", "deadlines"))
@router.message(F.text == BTN_EXAMS)
async def on_exams(message: Message, sf, settings: Settings) -> None:
    me = await _need_me(message, sf, settings)
    if me is None:
        return
    today = today_msk()
    async with sf() as session:
        items = await upcoming_exams(session, me[1].study_group_id, today)
    days = me[0].exam_reminder_days
    tail = f"\n\nНапомню за {days} дн. — поменять в /settings." if days else "\n\nНапоминания выключены — включить в /settings."
    await message.answer(format_exams(items, today=today) + tail)


# --- календарь ----------------------------------------------------------------

GUIDE_TAIL = (
    "\n\nГотово: занятия появятся в календаре и будут обновляться сами. "
    "О переносах я всё равно напишу сюда сразу — календари подтягивают подписку с задержкой."
)


def calendar_text(link: TelegramLink, settings: Settings) -> str:
    return (
        "<b>Расписание в календаре телефона</b>\n\n"
        "<b>Подписка</b> — календарь сам подтягивает переносы и отмены.\n"
        f"Ваша ссылка (нажмите, чтобы скопировать):\n<code>{escape(settings.feed_url(link.feed_token))}</code>\n\n"
        "⚠️ Не открывайте её в браузере: телефон скачает файл, и он не будет обновляться. "
        "Ссылку добавляют <b>внутри календаря</b> — выберите свой, там 3–4 шага.\n\n"
        "Ссылка личная: у любого, кто её получит, будет ваше расписание. Утекла — «Новая ссылка».\n\n"
        "<b>Файл .ics</b> — разовый импорт на три недели вперёд, сам не обновляется."
    )


def guide_text(platform: str, link: TelegramLink, settings: Settings) -> str:
    url = f"<code>{escape(settings.feed_url(link.feed_token))}</code>"
    if platform == "ios":
        return (
            "<b>🍏 iPhone / iPad</b>\n\n"
            f"1. Скопируйте ссылку:\n{url}\n"
            "2. <b>Настройки → Приложения → Календарь → Учётные записи</b> "
            "(на iOS 17 и старше: Настройки → Календарь → Учётные записи).\n"
            "3. <b>Новая учётная запись → Другое → Подписной календарь</b>.\n"
            "4. Вставьте ссылку → «Далее» → «Сохранить».\n\n"
            "Короче: вставьте эту ссылку в адресную строку Safari — он сам предложит подписаться:\n"
            f"<code>{escape(settings.webcal_url(link.feed_token))}</code>" + GUIDE_TAIL
        )
    if platform == "android":
        return (
            "<b>🤖 Android (Google Календарь)</b>\n\n"
            "В приложении подписку добавить нельзя — только на сайте, один раз.\n\n"
            f"1. Скопируйте ссылку:\n{url}\n"
            "2. Откройте <b>calendar.google.com</b> под тем же аккаунтом (с телефона — «Версия для ПК»).\n"
            "3. Слева <b>«Другие календари» → «+» → «Добавить по URL»</b> → вставьте ссылку.\n"
            f"4. На телефоне: Google Календарь → ☰ → Настройки → «{ics.CALENDAR_NAME}» → включите "
            "<b>«Синхронизация»</b>.\n\nGoogle обновляет подписку раз в несколько часов." + GUIDE_TAIL
        )
    if platform == "mac":
        return (
            "<b>💻 Mac</b>\n\n"
            f"1. Скопируйте ссылку:\n{url}\n"
            "2. Календарь → <b>Файл → Новая подписка на календарь</b> → вставьте ссылку.\n"
            "3. «Автообновление» — каждый час, «Расположение» — iCloud, чтобы подписка была и на iPhone."
            + GUIDE_TAIL
        )
    return (
        "<b>📧 Outlook</b>\n\n"
        f"1. Скопируйте ссылку:\n{url}\n"
        "2. <b>outlook.com</b> → Календарь → «Добавить календарь» → <b>«Подписка из Интернета»</b>.\n"
        "3. Вставьте ссылку → «Импорт».\n\nOutlook обновляет подписку редко, бывает раз в сутки." + GUIDE_TAIL
    )


def calendar_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for platform, label in (("ios", "🍏 iPhone"), ("android", "🤖 Android"), ("mac", "💻 Mac"), ("outlook", "📧 Outlook")):
        b.button(text=label, callback_data=f"cal:{platform}")
    b.button(text="📎 Файл .ics", callback_data="cal:file")
    b.button(text="🔗 Новая ссылка", callback_data="cal:rotate")
    b.adjust(2, 2, 2)
    return b.as_markup()


def _back_cal() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="← Календарь", callback_data="cal:main")
    return b.as_markup()


@router.message(Command("calendar"))
@router.message(F.text == BTN_CAL)
async def on_calendar(message: Message, sf, settings: Settings) -> None:
    me = await _need_me(message, sf, settings)
    if me is None:
        return
    await message.answer(calendar_text(me[0], settings), reply_markup=calendar_kb())


@router.callback_query(F.data.startswith("cal:"))
async def on_calendar_action(query: CallbackQuery, sf, settings: Settings) -> None:
    action = query.data[4:]
    async with sf() as session:
        me = await links.by_telegram(session, query.from_user.id)
        if me is None:
            await query.answer("Подключите бота из профиля кабинета", show_alert=True)
            return
        link, student = me
        if action == "file":
            today = today_msk()
            body = await ics.build_calendar(session, student.study_group_id, start=today, end=today + ics.FILE_AHEAD,
                                            for_subscription=False, reminder_minutes=link.reminder_minutes)
            await query.message.answer_document(
                BufferedInputFile(body, filename=f"mgimo-{today:%Y%m%d}.ics"),
                caption="Откройте файл — телефон предложит добавить события в календарь. Сам он не обновляется: "
                        "для этого — подписка по ссылке.",
            )
            await query.answer()
            return
        if action == "rotate":
            links.rotate_feed_token(link)
            await session.commit()
            await query.answer("Ссылка перевыпущена")
            await query.message.answer(
                "Новая ссылка — старая больше не работает, переподпишите календарь:\n"
                f"<code>{escape(settings.feed_url(link.feed_token))}</code>"
            )
            return
    if action in ("ios", "android", "mac", "outlook"):
        await _edit(query, guide_text(action, link, settings), _back_cal())
    elif action == "main":
        await _edit(query, calendar_text(link, settings), calendar_kb())
    else:
        await query.answer()


# --- справки ------------------------------------------------------------------


@router.message(Command("certificates"))
@router.message(F.text == BTN_CERTS)
async def on_certificates(message: Message, sf, settings: Settings) -> None:
    me = await _need_me(message, sf, settings)
    if me is None:
        return
    async with sf() as session:
        rows = (await certs.list_requests(session, me[1].id))[:10]
    base = escape(settings.public_base_url.rstrip("/"))
    if not rows:
        await message.answer(f"Заявок на справки нет. Заказать — в кабинете: <a href=\"{base}/mfc/certificates\">Справки</a>.")
        return
    lines = ["<b>Ваши заявки на справки</b>\n"]
    for r in rows:
        line = f"№{r.id} · {escape(CertificateKind.TITLES.get(r.kind, r.kind))}\n    <b>{escape(certs.electronic_label(r))}</b>"
        paper = certs.paper_label(r)
        if paper and r.paper_status != "none":
            line += f"\n    {escape(paper)}"
        lines.append(line)
    lines.append(f"\nПодробности и скачивание — в <a href=\"{base}/mfc/certificates\">кабинете</a>. "
                 "О смене статуса напишу сам.")
    await message.answer("\n".join(lines))


# --- настройки ----------------------------------------------------------------


def settings_view(link: TelegramLink) -> tuple[str, InlineKeyboardMarkup]:
    digest = link.digest_at or "выключена"
    reminder = f"за {link.reminder_minutes} мин" if link.reminder_minutes else "нет"
    exams = f"за {link.exam_reminder_days} дн." if link.exam_reminder_days else "выключены"
    text = (
        "<b>Настройки</b>\n\n"
        f"Утренняя сводка: <b>{digest}</b>\n"
        f"Изменения расписания: <b>{'присылать' if link.notify_changes else 'не присылать'}</b>\n"
        f"Статусы справок: <b>{'присылать' if link.notify_certificates else 'не присылать'}</b>\n"
        f"Напоминания о зачётах и экзаменах: <b>{exams}</b>\n"
        f"Напоминание о паре в календаре: <b>{reminder}</b>\n\n"
        "Напоминания о паре работают в файле .ics и в Apple Calendar; Google в подписках их "
        "игнорирует — там время задаётся в настройках самого календаря."
    )
    b = InlineKeyboardBuilder()
    for opt in DIGEST_OPTIONS:
        b.button(text=("• " if opt == link.digest_at else "") + (opt or "выкл"), callback_data=f"set:digest:{opt or 'off'}")
    b.button(text=("🔔" if link.notify_changes else "🔕") + " Изменения", callback_data="set:changes:toggle")
    b.button(text=("🔔" if link.notify_certificates else "🔕") + " Справки", callback_data="set:certs:toggle")
    for opt in EXAM_OPTIONS:
        b.button(text=("• " if opt == link.exam_reminder_days else "") + (f"зачёт −{opt} дн" if opt else "без"),
                 callback_data=f"set:exam:{opt or 0}")
    for opt in REMINDER_OPTIONS:
        b.button(text=("• " if opt == link.reminder_minutes else "") + (f"пара −{opt} мин" if opt else "без"),
                 callback_data=f"set:remind:{opt or 0}")
    b.button(text="🔗 Перевыпустить ссылку календаря", callback_data="cal:rotate")
    b.button(text="🚪 Отключить бота", callback_data="logout:ask")
    b.adjust(len(DIGEST_OPTIONS), 2, len(EXAM_OPTIONS), len(REMINDER_OPTIONS), 1, 1)
    return text, b.as_markup()


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def on_settings(message: Message, sf, settings: Settings) -> None:
    me = await _need_me(message, sf, settings)
    if me is None:
        return
    text, kb = settings_view(me[0])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("set:"))
async def on_setting(query: CallbackQuery, sf) -> None:
    _, action, value = (query.data.split(":") + ["", ""])[:3]
    async with sf() as session:
        me = await links.by_telegram(session, query.from_user.id)
        if me is None:
            await query.answer("Подключите бота из профиля кабинета", show_alert=True)
            return
        link = me[0]
        # Значения — только из клавиатуры: callback_data присылает клиент.
        if action == "digest" and (value == "off" or value in DIGEST_OPTIONS):
            link.digest_at = None if value == "off" else value
        elif action == "changes" and value == "toggle":
            link.notify_changes = not link.notify_changes
        elif action == "certs" and value == "toggle":
            link.notify_certificates = not link.notify_certificates
        elif action == "exam" and value.isdigit() and (int(value) or None) in EXAM_OPTIONS:
            link.exam_reminder_days = int(value) or None
        elif action == "remind" and value.isdigit() and (int(value) or None) in REMINDER_OPTIONS:
            link.reminder_minutes = int(value) or None
        else:
            await query.answer()
            return
        await session.commit()
    text, kb = settings_view(link)
    await _edit(query, text, kb)


# --- статус и выход -----------------------------------------------------------


@router.message(Command("status"))
async def on_status(message: Message, sf) -> None:
    async with sf() as session:
        run = await session.scalar(
            select(ImportRun).where(ImportRun.status.in_([ImportStatus.OK, ImportStatus.PARTIAL]))
            .order_by(ImportRun.finished_at.desc()).limit(1)
        )
    if run is None or run.finished_at is None:
        await message.answer("Расписание ещё ни разу не загружалось из РУЗ.")
        return
    finished = run.finished_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(MOSCOW)
    await message.answer(
        f"Расписание последний раз загружено из РУЗ МГИМО <b>{finished:%d.%m.%Y в %H:%M}</b> (МСК), "
        f"период {run.period_start:%d.%m}–{run.period_end:%d.%m}."
        + (" Часть групп тогда не загрузилась." if run.status == ImportStatus.PARTIAL else "")
    )


@router.message(Command("logout"))
async def on_logout(message: Message) -> None:
    b = InlineKeyboardBuilder()
    b.button(text="Да, отключить", callback_data="logout:yes")
    b.button(text="Отмена", callback_data="logout:no")
    await message.answer(
        "Отключить бота? Я удалю привязку к кабинету, ссылку на календарь (подписка перестанет "
        "обновляться) и историю напоминаний. Подключить снова можно из профиля.",
        reply_markup=b.as_markup(),
    )


@router.callback_query(F.data.startswith("logout:"))
async def on_logout_action(query: CallbackQuery, sf, settings: Settings) -> None:
    action = query.data.split(":")[1]
    if action == "ask":
        await on_logout(query.message)
        await query.answer()
        return
    if action != "yes":
        await _edit(query, "Хорошо, ничего не трогаю.", None)
        return
    async with sf() as session:
        me = await links.by_telegram(session, query.from_user.id)
        if me is not None:
            await links.unlink(session, me[1].id)
            await session.commit()
    await _edit(query, "Готово: привязка, ссылка на календарь и напоминания удалены. Бот больше ничего о вас не хранит.\n\n"
                + not_linked_text(settings), None)


@router.message(F.text)
async def on_unknown(message: Message) -> None:
    await message.answer("Не понял. Всё — кнопками внизу или /help.", reply_markup=MAIN_KB)
