"""grades: disciplines, courses, enrollments, assessments, scores, final grades, events; users.teacher_id

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-09 16:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch:
        batch.add_column(sa.Column('teacher_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_users_teacher_id', 'teachers', ['teacher_id'], ['id'])
        batch.create_unique_constraint('uq_users_teacher_id', ['teacher_id'])

    op.create_table(
        'disciplines',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'courses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('discipline_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('subgroup', sa.String(length=100), nullable=False),
        sa.Column('teacher_id', sa.Integer(), nullable=True),
        sa.Column('semester', sa.Integer(), nullable=False),
        sa.Column('academic_year', sa.String(length=9), nullable=False),
        sa.Column('control_form', sa.String(length=20), nullable=False),
        sa.Column('hours', sa.Integer(), nullable=False),
        sa.Column('credits', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['discipline_id'], ['disciplines.id']),
        sa.ForeignKeyConstraint(['group_id'], ['study_groups.id']),
        sa.ForeignKeyConstraint(['teacher_id'], ['teachers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('discipline_id', 'group_id', 'semester', 'subgroup', name='uq_courses_ledger'),
    )
    op.create_index('ix_courses_group_id', 'courses', ['group_id'])
    op.create_index('ix_courses_teacher_id', 'courses', ['teacher_id'])
    op.create_table(
        'course_enrollments',
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('course_id', 'student_id'),
    )
    op.create_index('ix_course_enrollments_student', 'course_enrollments', ['student_id', 'course_id'])
    op.create_table(
        'assessments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('max_points', sa.Float(), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_assessments_course_id', 'assessments', ['course_id'])
    op.create_table(
        'scores',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('assessment_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('points', sa.Float(), nullable=False),
        sa.Column('comment', sa.String(length=500), nullable=False),
        sa.Column('graded_by', sa.Integer(), nullable=True),
        sa.Column('graded_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['assessment_id'], ['assessments.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.ForeignKeyConstraint(['graded_by'], ['teachers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('assessment_id', 'student_id', name='uq_scores_assessment_student'),
    )
    op.create_index('ix_scores_student_id', 'scores', ['student_id'])
    op.create_table(
        'final_grades',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('current_points', sa.Float(), nullable=False),
        sa.Column('exam_points', sa.Float(), nullable=True),
        sa.Column('total_points', sa.Float(), nullable=False),
        sa.Column('result', sa.String(length=10), nullable=False),
        sa.Column('teacher_id', sa.Integer(), nullable=True),
        sa.Column('graded_on', sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id']),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.ForeignKeyConstraint(['teacher_id'], ['teachers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('course_id', 'student_id', 'attempt', name='uq_final_grades_attempt'),
    )
    op.create_index('ix_final_grades_course_id', 'final_grades', ['course_id'])
    op.create_index('ix_final_grades_student_id', 'final_grades', ['student_id'])
    op.create_table(
        'grade_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('course_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('text', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('notified_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_grade_events_student_created', 'grade_events', ['student_id', 'created_at'])
    op.create_index('ix_grade_events_notified_at', 'grade_events', ['notified_at'])


def downgrade() -> None:
    op.drop_table('grade_events')
    op.drop_table('final_grades')
    op.drop_table('scores')
    op.drop_table('assessments')
    op.drop_table('course_enrollments')
    op.drop_table('courses')
    op.drop_table('disciplines')
    with op.batch_alter_table('users') as batch:
        batch.drop_constraint('uq_users_teacher_id', type_='unique')
        batch.drop_constraint('fk_users_teacher_id', type_='foreignkey')
        batch.drop_column('teacher_id')
