"""Поддельный сервер РУЗ для тестов.

Данные целиком выдуманы. «Реальные» ФИО и email преподавателей здесь — маркеры,
по которым тесты проверяют, что они не доезжают до БД.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

REAL_NAME_MARKERS = ("Настоящий Препод Тестович", "Подлинная Лектор Маркеровна", "real.teacher@example.org")

FACULTIES = [
    {"facultyOid": 24, "abbr": "Ф-т МО", "name": "Факультет тестовых отношений", "lecturer": REAL_NAME_MARKERS[0]},
]


LECTURERS = [
    {"lecturerOid": 501, "fio": REAL_NAME_MARKERS[0], "shortFIO": "Настоящий П.Т.", "email": REAL_NAME_MARKERS[2]},
    {"lecturerOid": 502, "fio": REAL_NAME_MARKERS[1], "shortFIO": "Подлинная Л.М."},
]


def group(oid: int, number: str, name: str, faculty: int = 24) -> dict:
    return {
        "groupOid": oid,
        "number": number,
        "name": name,
        "facultyOid": faculty,
        "course": 1,
        "formOfEducation": "Очная",
        "speciality": "Тестовая специальность",
        "groupSpecialization_name": "Тестовая специализация",
        "is_schedule": 1,
    }


def lesson(
    oid: int,
    day: date,
    begin: str = "09:00",
    end: str = "10:20",
    *,
    discipline: str = "Введение в тестирование",
    kind: str = "Лекция",
    auditorium: str = "101",
    lecturer_oid: int = 501,
    lecturer: str = REAL_NAME_MARKERS[0],
) -> dict:
    return {
        "lessonOid": oid,
        "date": day.isoformat(),
        "beginLesson": begin,
        "endLesson": end,
        "lessonNumberStart": 1,
        "discipline": discipline,
        "kindOfWork": kind,
        "auditorium": auditorium,
        "building": "Корпус А",
        "stream": None,
        "subGroup": None,
        "modifieddate": "2026-06-03T11:38:16Z00:00",
        "lecturerOid": lecturer_oid,
        "lecturer": lecturer,
        "lecturerEmail": REAL_NAME_MARKERS[2],
        "lecturer_rank": "доцент",
        "author": lecturer,
        "note": f"Замена: {lecturer}",
        "listOfLecturers": [
            {"lecturerOid": lecturer_oid, "lecturer": lecturer, "lecturerEmail": REAL_NAME_MARKERS[2]}
        ],
    }


class FakeRuz:
    def __init__(self) -> None:
        self.groups: list[dict] = []
        self.schedules: dict[int, list[dict]] = {}
        self.failing: set[int] = set()
        self.requests: list[str] = []
        self.lecturers: list[dict] = list(LECTURERS)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path.endswith("/dictionary/faculties"):
            return self._json(FACULTIES)
        if path.endswith("/dictionary/lecturers"):
            return self._json(self.lecturers)
        if path.endswith("/dictionary/groups"):
            return self._json(self.groups)
        if "/schedule/group/" in path:
            group_id = int(path.rsplit("/", 1)[1])
            if group_id in self.failing:
                return httpx.Response(503)
            return self._json(self.schedules.get(group_id, []))
        return httpx.Response(404)

    @staticmethod
    def _json(data: object) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(data, ensure_ascii=False).encode())
