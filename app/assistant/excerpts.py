"""Доменное правило: нормативный пункт нельзя разрезать на границе страницы."""
from bisect import bisect_right
from dataclasses import dataclass
import re


class ExcerptTooLong(ValueError):
    pass


@dataclass(frozen=True)
class Excerpt:
    text: str
    page: int
    page_end: int


def document_excerpts(pages: list[str], *, maximum: int = 6000) -> list[Excerpt]:
    text = ""
    starts = []
    for page in pages:
        starts.append(len(text))
        text += page + "\n\n"
    boundaries = sorted({0, len(text), *[m.start() for m in
        re.finditer(r"(?m)^\s*\d+(?:\.\d+)*[.)]\s+", text)]})
    result = []
    start = 0
    for end in boundaries[1:]:
        block = text[start:end].strip()
        if len(block) < 30:
            continue  # Короткий заголовок относится к следующему пункту.
        if len(block) > maximum:
            raise ExcerptTooLong("Нормативный пункт превышает лимит выдержки; нужна ручная подготовка")
        result.append(Excerpt(block, bisect_right(starts, start), bisect_right(starts, end - 1)))
        start = end
    if start < len(text) and text[start:].strip():
        if not result or len(result[-1].text + text[start:]) > maximum:
            raise ExcerptTooLong("Короткий фрагмент нельзя безопасно отделить от нормативного пункта")
        previous = result.pop()
        result.append(Excerpt(previous.text + "\n" + text[start:].strip(), previous.page, len(pages)))
    return result
