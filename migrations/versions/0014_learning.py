"""Сдача заданий, сообщения и посещаемость."""
from alembic import op
import sqlalchemy as sa
revision = "0014_learning"
down_revision = "0013_merge"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('submissions',
        sa.Column('assessment_id', sa.Integer(), sa.ForeignKey('assessments.id'), primary_key=True, nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), primary_key=True, nullable=False),
        sa.Column('text', sa.Text(), primary_key=False, nullable=False),
        sa.Column('filename', sa.String(length=200), primary_key=False, nullable=False),
        sa.Column('data', sa.LargeBinary(), primary_key=False, nullable=True),
        sa.Column('revision', sa.Integer(), primary_key=False, nullable=False),
        sa.Column('status', sa.String(length=20), primary_key=False, nullable=False),
        sa.Column('updated_at', sa.DateTime(), primary_key=False, nullable=False),
    )
    op.create_table('task_messages',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('assessment_id', sa.Integer(), sa.ForeignKey('assessments.id'), primary_key=False, nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), primary_key=False, nullable=True),
        sa.Column('author_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
        sa.Column('body', sa.Text(), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(), primary_key=False, nullable=False),
    )
    op.create_index('ix_task_messages_student_id', 'task_messages', ['student_id'])
    op.create_index('ix_task_messages_assessment_id', 'task_messages', ['assessment_id'])
    op.create_table('learning_notices',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
        sa.Column('title', sa.String(length=300), primary_key=False, nullable=False),
        sa.Column('body', sa.Text(), primary_key=False, nullable=False),
        sa.Column('href', sa.String(length=200), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(), primary_key=False, nullable=False),
        sa.Column('telegram_state', sa.String(length=20), primary_key=False, nullable=False),
        sa.Column('attempts', sa.Integer(), primary_key=False, nullable=False),
        sa.Column('attempted_at', sa.DateTime(), primary_key=False, nullable=True),
        sa.Column('sent_at', sa.DateTime(), primary_key=False, nullable=True),
    )
    op.create_index('ix_learning_notices_user_id', 'learning_notices', ['user_id'])
    op.create_index('ix_learning_notices_created_at', 'learning_notices', ['created_at'])
    op.create_table('attendance_windows',
        sa.Column('lesson_id', sa.Integer(), sa.ForeignKey('lessons.id'), primary_key=True, nullable=False),
        sa.Column('teacher_id', sa.Integer(), sa.ForeignKey('teachers.id'), primary_key=False, nullable=False),
        sa.Column('salt', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('code_hash', sa.String(length=64), primary_key=False, nullable=False),
        sa.Column('opened_at', sa.DateTime(), primary_key=False, nullable=False),
        sa.Column('expires_at', sa.DateTime(), primary_key=False, nullable=False),
    )
    op.create_table('attendance',
        sa.Column('lesson_id', sa.Integer(), sa.ForeignKey('lessons.id'), primary_key=True, nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), primary_key=True, nullable=False),
        sa.Column('present', sa.Boolean(), primary_key=False, nullable=False),
        sa.Column('corrected_by', sa.Integer(), sa.ForeignKey('teachers.id'), primary_key=False, nullable=True),
        sa.Column('reason', sa.String(length=500), primary_key=False, nullable=False),
        sa.Column('updated_at', sa.DateTime(), primary_key=False, nullable=False),
    )
    op.create_table('attendance_attempts',
        sa.Column('lesson_id', sa.Integer(), sa.ForeignKey('lessons.id'), primary_key=True, nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), primary_key=True, nullable=False),
        sa.Column('opened_at', sa.DateTime(), primary_key=False, nullable=False),
        sa.Column('attempts', sa.Integer(), primary_key=False, nullable=False),
    )
    op.create_table('attendance_changes',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('lesson_id', sa.Integer(), sa.ForeignKey('lessons.id'), primary_key=False, nullable=False),
        sa.Column('student_id', sa.Integer(), sa.ForeignKey('students.id'), primary_key=False, nullable=False),
        sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), primary_key=False, nullable=False),
        sa.Column('present', sa.Boolean(), primary_key=False, nullable=False),
        sa.Column('reason', sa.String(length=500), primary_key=False, nullable=False),
        sa.Column('created_at', sa.DateTime(), primary_key=False, nullable=False),
    )
    op.create_index('ix_attendance_changes_lesson_id', 'attendance_changes', ['lesson_id'])

def downgrade():
    op.drop_table('attendance_changes')
    op.drop_table('attendance_attempts')
    op.drop_table('attendance')
    op.drop_table('attendance_windows')
    op.drop_table('learning_notices')
    op.drop_table('task_messages')
    op.drop_table('submissions')
