"""calendar layers: elective choices, activities, activity signups

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06 12:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'elective_choices',
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('value', sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.PrimaryKeyConstraint('student_id', 'kind', 'value'),
    )
    op.create_table(
        'activities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('starts_at', sa.DateTime(), nullable=False),
        sa.Column('ends_at', sa.DateTime(), nullable=False),
        sa.Column('place', sa.String(length=300), nullable=False),
        sa.Column('organizer', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('title', 'starts_at', name='uq_activities_title_start'),
    )
    op.create_index('ix_activities_starts_at', 'activities', ['starts_at'])
    op.create_table(
        'activity_signups',
        sa.Column('student_id', sa.Integer(), nullable=False),
        sa.Column('activity_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['student_id'], ['students.id']),
        sa.ForeignKeyConstraint(['activity_id'], ['activities.id']),
        sa.PrimaryKeyConstraint('student_id', 'activity_id'),
    )


def downgrade() -> None:
    op.drop_table('activity_signups')
    op.drop_index('ix_activities_starts_at', table_name='activities')
    op.drop_table('activities')
    op.drop_table('elective_choices')
