"""Общее SQL-правило актуального итога: старшая попытка, а не последняя дата записи."""
from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from app.models.grades import FinalGrade


def latest_final_clause():
    later = aliased(FinalGrade)
    return ~exists(select(later.id).where(
        later.course_id == FinalGrade.course_id,
        later.student_id == FinalGrade.student_id,
        later.attempt > FinalGrade.attempt,
    ).correlate(FinalGrade))
