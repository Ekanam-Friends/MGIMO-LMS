"""Балльно-рейтинговая шкала прототипа (решение docs/decisions/0007-grades.md).

Условная, но типичная для российских вузов схема на 100 баллов:

- экзамен и зачёт с оценкой: накоп за семестр до 60 баллов + экзамен до 40;
  к экзамену допускается студент с накопом от 30;
- зачёт: всё 100 баллов — текущий контроль, «зачтено» от 55.

Итог по сумме: 85+ «отлично», 70+ «хорошо», 55+ «удовлетворительно», иначе «неуд».
Все пороги — здесь, чтобы вуз мог поменять их в одном месте.
"""

from __future__ import annotations

from datetime import date

from app.models.grades import ControlForm, FinalResult

CURRENT_MAX = {ControlForm.EXAM: 60.0, ControlForm.DIFF_CREDIT: 100.0, ControlForm.CREDIT: 100.0}
EXAM_MAX = 40.0
ADMISSION_MIN = 30.0
"""Минимальный накоп для допуска к экзамену."""
PASS_MIN = 55.0
THRESHOLDS = ((85.0, FinalResult.EXCELLENT), (70.0, FinalResult.GOOD), (55.0, FinalResult.SATISFACTORY))


def current_max(control_form: str) -> float:
    return CURRENT_MAX.get(control_form, 100.0)


def suggest(control_form: str, current: float, exam: float | None = None) -> str:
    """Итог, который следует из баллов. Преподаватель может выставить другой."""
    total = current + (exam or 0.0)
    if control_form == ControlForm.CREDIT:
        return FinalResult.PASS if total >= PASS_MIN else FinalResult.NO_PASS
    for threshold, result in THRESHOLDS:
        if total >= threshold:
            return result
    return FinalResult.FAIL


def allowed_results(control_form: str) -> tuple[str, ...]:
    if control_form == ControlForm.CREDIT:
        return (FinalResult.PASS, FinalResult.NO_PASS, FinalResult.ABSENT)
    return (FinalResult.EXCELLENT, FinalResult.GOOD, FinalResult.SATISFACTORY, FinalResult.FAIL,
            FinalResult.ABSENT)


def tone(result: str | None) -> str:
    """Цвет плашки результата: green / blue / orange / red / grey."""
    if result in (FinalResult.EXCELLENT, FinalResult.PASS):
        return "green"
    if result == FinalResult.GOOD:
        return "blue"
    if result == FinalResult.SATISFACTORY:
        return "orange"
    if result in FinalResult.NEGATIVE:
        return "red"
    return "grey"


def percent_tone(percent: float | None) -> str:
    if percent is None:
        return "grey"
    if percent >= 85:
        return "green"
    if percent >= 70:
        return "blue"
    if percent >= 55:
        return "orange"
    return "red"


def academic_year(today: date) -> str:
    """Учебный год, в котором лежит дата: с 1 сентября по 31 августа."""
    start = today.year if today.month >= 9 else today.year - 1
    return f"{start}/{start + 1}"


def is_autumn(today: date) -> bool:
    """Осенний семестр — сентябрь…январь (с зимней сессией), весенний — февраль…август."""
    return today.month >= 9 or today.month == 1


def semester_number(course_year: int, today: date) -> int:
    """Номер текущего семестра студента: 1-й курс осенью — 1, весной — 2."""
    return 2 * course_year - (1 if is_autumn(today) else 0)


def fmt_points(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}".rstrip("0").rstrip(".").replace(".", ",")
