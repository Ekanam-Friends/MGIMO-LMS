from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, time

import httpx
import pytest

from app.schedule.fake_names import allowed_surnames, fake_teacher_name
from app.schedule.normalize import group_from_raw, lesson_from_raw, real_surnames
from app.schedule.ruz_client import RuzClient, RuzError
from tests.fake_ruz import REAL_NAME_MARKERS, group, lesson


def test_lesson_keeps_only_allowlisted_fields():
    data = lesson_from_raw(lesson(42, date(2026, 11, 2), "09:00", "10:20"))
    assert data.id == 42
    assert (data.date, data.begins_at, data.ends_at) == (date(2026, 11, 2), time(9), time(10, 20))
    assert data.ruz_modified_at == datetime(2026, 6, 3, 11, 38, 16)
    assert data.lecturer_oids == (501,)
    flat = repr(asdict(data))
    for marker in REAL_NAME_MARKERS:
        assert marker not in flat


def test_lesson_without_list_of_lecturers_uses_lecturer_oid():
    raw = lesson(1, date(2026, 11, 2))
    raw["listOfLecturers"] = []
    assert lesson_from_raw(raw).lecturer_oids == (501,)


def test_group_number_falls_back_to_name():
    raw = group(1, "", "26МО-01")
    assert group_from_raw(raw).number == "26МО-01"


def test_fake_name_is_deterministic_and_salted():
    assert fake_teacher_name(501, "s") == fake_teacher_name(501, "s")
    names = {fake_teacher_name(oid, "s") for oid in range(1, 200)}
    assert len(names) > 150
    assert all(len(name.split()) == 3 for name in names)


def test_allowed_surnames_blocks_both_genders():
    allowed = allowed_surnames(["Харитонова", "Покровский", "Настоящий"])
    assert "Харитонов" not in allowed
    assert "Покровский" not in allowed
    assert "Ильин" in allowed
    name = fake_teacher_name(1, "s", surnames=("Ильин",))
    assert name.split()[0] in ("Ильин", "Ильина")


def test_real_surnames_takes_first_word():
    assert real_surnames([{"fio": "Фамилия Имя Отчество"}, {"shortFIO": "Другая И.О."}, {}]) == {
        "Фамилия",
        "Другая",
    }


def test_fake_name_avoids_taken():
    first = fake_teacher_name(501, "s")
    assert fake_teacher_name(501, "s", {first}) != first


def test_fake_female_name_is_consistent():
    for oid in range(1, 300):
        surname, _, patronymic = fake_teacher_name(oid, "s").split()
        if patronymic.endswith("вна"):
            assert surname.endswith(("а", "ая"))


async def test_client_retries_server_errors_and_raises_on_error_payload():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path.endswith("faculties"):
            return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, json=[])
        return httpx.Response(200, json={"error": 1, "message": "Слишком короткая строка поиска"})

    async with RuzClient("https://ruz.test/api/", delay=0, backoff=0, transport=httpx.MockTransport(handler)) as c:
        assert await c.faculties() == []
        with pytest.raises(RuzError):
            await c.groups()
