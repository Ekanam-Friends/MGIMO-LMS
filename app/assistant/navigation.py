"""Навигация по явной просьбе: адреса только из серверного каталога."""
import re
from app.web.nav import POPULAR_SERVICES
ALIASES = {
 "посещаемость": "Отметка на паре", "отметку на паре": "Отметка на паре",
 "отметка на паре": "Отметка на паре", "отметиться на паре": "Отметка на паре",
 "задания": "СДО", "домашние задания": "СДО", "мудл": "СДО", "сдо": "СДО",
 "библиотеку": "Библиотека", "почту": "Почта", "диск": "Диск",
 "антиплагиат": "Антиплагиат", "ассистента": "ИИ-ассистент", "ии-ассистента": "ИИ-ассистент",
 "телеграм": "Расписание в Telegram", "telegram": "Расписание в Telegram",
 "бота": "Расписание в Telegram", "max": "Расписание в MAX", "макс": "Расписание в MAX",
 "календарь": "Календарь .ics", "приложение мгимо": "Приложение МГИМО",
}
def requested_service(question, role):
    q = re.sub(r"[.!?]+$", "", question.strip().lower().replace("ё", "е"))
    match = re.fullmatch(r"(?:пожалуйста,? )?(?:(?:можешь|можете) )?(?:открой|откройте|открыть|перейди|перейдите)(?:,? пожалуйста,?)? (?:мне )?(?:в |на |сервис )?(.+?)(?:,? пожалуйста)?", q)
    if not match:
        return None
    name = match[1].strip()
    titles = {title.lower(): title for title, _, _ in POPULAR_SERVICES}
    title = ALIASES.get(name) or titles.get(name)
    links = {title: href for title, _, href in POPULAR_SERVICES}
    links["Приложение МГИМО"] = "/services#app"
    if title == "Отметка на паре" and role not in {"student", "teacher"}:
        return None
    return {"title": title, "href": links[title]} if title in links else None
