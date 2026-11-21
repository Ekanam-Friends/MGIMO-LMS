"""Общие фикстуры: свежий кэш на каждый тест (решение 0027).

Кэш процесса глобальный: без сброса запись одного теста (одни и те же id студентов в разных
временных базах) могла бы попасть в другой тест.
"""

import pytest

from app.cache import make_cache, set_cache


@pytest.fixture(autouse=True)
def fresh_cache():
    cache = make_cache()
    previous = set_cache(cache)
    yield cache
    set_cache(previous)
