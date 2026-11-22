"""PR №55: кнопки в «Заданиях» и «Отметке», ключи групп меню.

Кнопка без класса .btn в grid-форме растягивается на всю ширину и берёт серый фон браузера —
так и остались «Вернуть на доработку» и «Сохранить исправление» в первой версии PR.
Ключ группы меню хранится в localStorage: если брать название, оно переводится, и при смене
языка интерфейса раскрытые группы теряются.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.web.nav import MENU, STAFF_MENU, TEACHER_MENU

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"


def test_learning_buttons_have_btn_variant():
    buttons = [(path.name, tag) for path in sorted((TEMPLATES / "learning").glob("*.html"))
               for tag in re.findall(r"<button\b[^>]*>", path.read_text(encoding="utf-8"))]
    assert len(buttons) >= 7, "кнопок в learning/* стало меньше — проверьте, что тест не устарел"
    for template, tag in buttons:
        classes = re.search(r'class="([^"]*)"', tag)
        names = classes.group(1).split() if classes else []
        assert "btn" in names, f"{template}: {tag} без класса .btn"
        assert {"btn--primary", "btn--outline"} & set(names), f"{template}: {tag} без варианта кнопки"


def test_menu_groups_have_stable_untranslated_keys():
    for menu in (MENU, TEACHER_MENU, STAFF_MENU):
        keys = [item.get("key") for item in menu if item.get("children")]
        assert all(keys), "у каждой группы меню должен быть key"
        assert all(re.fullmatch(r"[a-z][a-z0-9-]*", key) for key in keys), keys
        assert len(keys) == len(set(keys)), keys
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'data-group="{{ item.key }}"' in base
