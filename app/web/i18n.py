"""Переводы интерфейса из локальных словарей; данные не отправляются переводчику при показе страниц."""
import json
from functools import lru_cache
from pathlib import Path

from jinja2 import pass_context

LANGUAGES = {"ru": "Русский", "en": "English", "fr": "Français", "zh": "简体中文"}


@lru_cache
def catalogue(language):
    if language == "ru" or language not in LANGUAGES:
        return {}
    path = Path(__file__).with_name("locales") / f"{language}.json"
    result = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    overrides = path.with_name("overrides.json")
    if overrides.exists():
        result.update(json.loads(overrides.read_text(encoding="utf-8")).get(language, {}))
    return result


def language_for(viewer):
    if viewer and viewer.user.role == "student":
        value = viewer.user.interface_language
        if value in LANGUAGES:
            return value
    return "ru"


def translate(value, language):
    if not isinstance(value, str) or not value.strip():
        return value
    key = value.strip()
    replacement = catalogue(language).get(key)
    if replacement is None:
        return value
    return value[:len(value)-len(value.lstrip())] + replacement + value[len(value.rstrip()):]


@pass_context
def gettext(context, value):
    return translate(value, language_for(context.get("viewer")))


def configure(env):
    env.globals.update(_=gettext, interface_language=lambda viewer: language_for(viewer),
                       LANGUAGES=LANGUAGES, interface_catalogue=lambda viewer: catalogue(language_for(viewer)))
    env.finalize = gettext
