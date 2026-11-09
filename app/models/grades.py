"""Успеваемость: курсы, контрольные точки, баллы накопа и итоговые оценки (зачётка).

Решение docs/decisions/0007-grades.md. Коротко:

- `courses` — дисциплина у конкретной группы (или подгруппы) в конкретном семестре с одним
  ведущим преподавателем. Это одна ведомость.
- `course_enrollments` — кто из студентов записан в ведомость. Явная запись, а не «вся группа»:
  языки идут по подгруппам, и студент сидит только в своей.
- `assessments` — контрольные точки текущего контроля: домашние задания, контрольные, работа на
  семинарах. Сумма их максимумов — максимум накопа по курсу.
- `scores` — баллы студента за контрольную точку. Одна строка на пару (точка, студент).
- `final_grades` — итог в зачётке: экзамен «5/4/3/2», зачёт/незачёт, неявка. Попытки
  (пересдачи) — отдельными строками с номером попытки.
- `grade_events` — журнал «что нового в успеваемости» для студента: лента в кабинете и очередь
  уведомлений (бот читает строки с пустым `notified_at`, как у справок).

Доступ: преподаватель работает только с курсами, где `courses.teacher_id` — его id из сессии;
студент видит только строки со своим `student_id`. Проверки — в запросах app/grades/service.py.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ControlForm:
    EXAM = "exam"
    CREDIT = "credit"
    """Зачёт без оценки: «зачтено» / «не зачтено»."""
    DIFF_CREDIT = "diff_credit"
    """Зачёт с оценкой: как экзамен по шкале, но без отдельного экзаменационного балла."""

    TITLES = {EXAM: "Экзамен", CREDIT: "Зачёт", DIFF_CREDIT: "Зачёт с оценкой"}


class AssessmentKind:
    HOMEWORK = "homework"
    TEST = "test"
    SEMINAR = "seminar"
    ESSAY = "essay"
    MIDTERM = "midterm"

    TITLES = {
        HOMEWORK: "Домашнее задание",
        TEST: "Контрольная работа",
        SEMINAR: "Работа на семинарах",
        ESSAY: "Эссе / доклад",
        MIDTERM: "Рубежный контроль",
    }


class FinalResult:
    EXCELLENT = "5"
    GOOD = "4"
    SATISFACTORY = "3"
    FAIL = "2"
    PASS = "pass"
    NO_PASS = "fail"
    ABSENT = "absent"

    TITLES = {
        EXCELLENT: "Отлично",
        GOOD: "Хорошо",
        SATISFACTORY: "Удовлетворительно",
        FAIL: "Неудовлетворительно",
        PASS: "Зачтено",
        NO_PASS: "Не зачтено",
        ABSENT: "Неявка",
    }
    NEGATIVE = frozenset({FAIL, NO_PASS, ABSENT})
    """Результаты, после которых по дисциплине висит долг (если нет успешной пересдачи)."""
    NUMERIC = {EXCELLENT: 5, GOOD: 4, SATISFACTORY: 3, FAIL: 2}


class CourseStatus:
    OPEN = "open"
    CLOSED = "closed"
    """Ведомость закрыта преподавателем: баллы и итог больше не меняются."""


class GradeEventKind:
    SCORE = "score"
    FINAL = "final"
    HOMEWORK = "homework"


class Discipline(Base):
    __tablename__ = "disciplines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), unique=True)
    """Как в РУЗ («Правоведение», «английский первый») или из учебного плана генератора."""


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discipline_id: Mapped[int] = mapped_column(ForeignKey("disciplines.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("study_groups.id"), index=True)
    subgroup: Mapped[str] = mapped_column(String(100), default="")
    """Пусто — вся группа. Иначе подгруппа («подгруппа 2»): у языков их несколько."""
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("teachers.id"), nullable=True, index=True)
    semester: Mapped[int] = mapped_column(Integer)
    """Номер семестра обучения группы: 1 — осень первого курса, 2 — весна первого и т. д."""
    academic_year: Mapped[str] = mapped_column(String(9))
    """«2026/2027»."""
    control_form: Mapped[str] = mapped_column(String(20), default=ControlForm.CREDIT)
    hours: Mapped[int] = mapped_column(Integer, default=72)
    credits: Mapped[int] = mapped_column(Integer, default=2)
    """Зачётные единицы."""
    status: Mapped[str] = mapped_column(String(20), default=CourseStatus.OPEN)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("discipline_id", "group_id", "semester", "subgroup", name="uq_courses_ledger"),
    )


class CourseEnrollment(Base):
    __tablename__ = "course_enrollments"

    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), primary_key=True)

    __table_args__ = (Index("ix_course_enrollments_student", "student_id", "course_id"),)


class Assessment(Base):
    __tablename__ = "assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default=AssessmentKind.HOMEWORK)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    """Текст задания — для домашних работ его видит студент."""
    max_points: Mapped[float] = mapped_column(Float, default=10)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    """Срок сдачи (для ДЗ) или дата проведения."""
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    points: Mapped[float] = mapped_column(Float)
    comment: Mapped[str] = mapped_column(String(500), default="")
    graded_by: Mapped[int | None] = mapped_column(ForeignKey("teachers.id"), nullable=True)
    graded_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (UniqueConstraint("assessment_id", "student_id", name="uq_scores_assessment_student"),)


class FinalGrade(Base):
    __tablename__ = "final_grades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    """1 — основная сдача, 2 и 3 — пересдачи."""
    current_points: Mapped[float] = mapped_column(Float, default=0)
    """Накоп на момент выставления (снимок: потом баллы могут поменяться)."""
    exam_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_points: Mapped[float] = mapped_column(Float, default=0)
    result: Mapped[str] = mapped_column(String(10))
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("teachers.id"), nullable=True)
    graded_on: Mapped[date] = mapped_column(Date)

    __table_args__ = (
        UniqueConstraint("course_id", "student_id", "attempt", name="uq_final_grades_attempt"),
    )


class GradeEvent(Base):
    __tablename__ = "grade_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    kind: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    __table_args__ = (Index("ix_grade_events_student_created", "student_id", "created_at"),)
