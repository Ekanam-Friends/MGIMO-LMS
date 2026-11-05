"""PDF справки по шаблону (решение 0003). В рабочей версии документ формировала бы 1С.

Документ нарочно не похож на настоящий: нет логотипа и полного наименования вуза,
вместо электронной подписи синий прямоугольник с подписью «имитация», по всему листу
пометка «концепт для конкурса, не является официальным документом».
"""

from __future__ import annotations

import io
import os
from datetime import date

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

from app.models.certificates import CertificateKind, CertificateRequest, Student

FONT = "LmsSans"
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/Library/Fonts/Arial.ttf",
)
DISCLAIMER = "Концепт для конкурса MGIMO×AI Challenge. Не является официальным документом."
BLUE = HexColor("#1f4fbf")

_font_ready = False


class FontNotFound(RuntimeError):
    pass


def _register_font(path: str = "") -> None:
    global _font_ready
    if _font_ready:
        return
    candidates = (path,) if path else FONT_CANDIDATES
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            pdfmetrics.registerFont(TTFont(FONT, candidate))
            _font_ready = True
            return
    raise FontNotFound(
        "Не найден TTF-шрифт с кириллицей. Задай PDF_FONT_PATH в .env "
        "(например, путь к DejaVuSans.ttf; в Debian — пакет fonts-dejavu-core)."
    )


def _d(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def body_lines(student: Student, request: CertificateRequest) -> list[str]:
    f = student.gender == "f"
    lines = [
        f"Дана {student.full_name}" + (f", {_d(student.birth_date)} г. р.," if student.birth_date else ","),
        f"в том, что {'она' if f else 'он'} действительно обучается на {student.course} курсе",
        f"({student.level}, {student.form_of_education} форма обучения, {student.education_basis} основа)",
        f"факультета: {student.faculty_name or '—'}.",
    ]
    if student.speciality:
        lines.append(f"Направление подготовки: {student.speciality}.")
    lines += [
        f"{'Зачислена' if f else 'Зачислен'} приказом {student.enrollment_order or '—'}"
        f" (дата зачисления {_d(student.enrollment_date)}).",
        f"Предполагаемый срок окончания обучения: {_d(student.expected_graduation)}.",
        f"Номер зачётной книжки: {student.record_book}.",
        "",
    ]
    if request.kind == CertificateKind.MILITARY:
        lines.append("Справка выдана для представления в военный комиссариат.")
    elif request.kind == CertificateKind.VISA:
        lines.append("Справка выдана для представления в посольство (консульство).")
    if request.purpose:
        lines.append(f"Место требования: {request.purpose}.")
    else:
        lines.append("Справка выдана для представления по месту требования.")
    return lines


def _wrap(canvas: Canvas, text: str, width: float, size: float) -> list[str]:
    words, out, line = text.split(" "), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if canvas.stringWidth(trial, FONT, size) <= width:
            line = trial
        else:
            out.append(line)
            line = word
    out.append(line)
    return out


def render(student: Student, request: CertificateRequest, *, issued: date, font_path: str = "") -> bytes:
    _register_font(font_path)
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4)
    width, height = A4
    left, right = 25 * mm, width - 20 * mm
    c.setTitle(f"{CertificateKind.TITLES.get(request.kind, 'Справка')} — заявка {request.id}")

    # Пометка-водяной знак по диагонали.
    c.saveState()
    c.setFillColor(Color(0.85, 0.1, 0.1, alpha=0.12))
    c.setFont(FONT, 30)
    c.translate(width / 2, height / 2)
    c.rotate(35)
    c.drawCentredString(0, 20, "КОНЦЕПТ ДЛЯ КОНКУРСА")
    c.drawCentredString(0, -20, "НЕ ЯВЛЯЕТСЯ ОФИЦИАЛЬНЫМ ДОКУМЕНТОМ")
    c.restoreState()

    c.setFont(FONT, 9)
    c.setFillColor(HexColor("#b00020"))
    c.drawCentredString(width / 2, height - 12 * mm, DISCLAIMER)
    c.setFillColor(HexColor("#000000"))

    y = height - 30 * mm
    c.setFont(FONT, 11)
    c.drawString(left, y, "Личный кабинет студента — прототип")
    y -= 6 * mm
    c.drawString(left, y, "Деканат (учебная часть)")
    y -= 6 * mm
    c.drawString(left, y, f"Исх. № ЛК-{request.id} от {_d(issued)}")

    y -= 18 * mm
    c.setFont(FONT, 16)
    c.drawCentredString(width / 2, y, CertificateKind.TITLES.get(request.kind, "Справка").upper())

    y -= 14 * mm
    c.setFont(FONT, 12)
    for paragraph in body_lines(student, request):
        if not paragraph:
            y -= 4 * mm
            continue
        for line in _wrap(c, paragraph, right - left, 12):
            c.drawString(left, y, line)
            y -= 6.5 * mm

    if request.copies > 1:
        y -= 2 * mm
        c.drawString(left, y, f"Экземпляров: {request.copies}.")
        y -= 6.5 * mm

    # Имитация электронной подписи.
    y -= 14 * mm
    box_w, box_h = 80 * mm, 26 * mm
    box_x = right - box_w
    c.drawString(left, y - box_h / 2, "Декан факультета")
    c.setStrokeColor(BLUE)
    c.setFillColor(Color(0.12, 0.31, 0.75, alpha=0.08))
    c.setLineWidth(1.5)
    c.roundRect(box_x, y - box_h, box_w, box_h, 3 * mm, stroke=1, fill=1)
    c.setFillColor(BLUE)
    c.setFont(FONT, 9)
    c.drawCentredString(box_x + box_w / 2, y - 7 * mm, "ДОКУМЕНТ ПОДПИСАН")
    c.drawCentredString(box_x + box_w / 2, y - 12 * mm, "ЭЛЕКТРОННОЙ ПОДПИСЬЮ")
    c.drawCentredString(box_x + box_w / 2, y - 19 * mm, "(имитация, подписи нет)")

    c.setFillColor(HexColor("#000000"))
    c.setFont(FONT, 10)
    c.drawString(left, 25 * mm, "Бумажный оригинал можно получить в деканате.")
    c.setFont(FONT, 8)
    c.setFillColor(HexColor("#b00020"))
    c.drawString(left, 15 * mm, DISCLAIMER + " Все данные вымышлены.")

    c.showPage()
    c.save()
    return buf.getvalue()
