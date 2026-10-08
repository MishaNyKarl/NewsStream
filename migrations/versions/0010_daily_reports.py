"""Daily report preferences and durable delivery queue."""
from alembic import op
import sqlalchemy as sa

revision = '0010_daily_reports'
down_revision = '0009_user_profiles'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('daily_reports',
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('minute', sa.Integer()),
        sa.Column('next_at', sa.DateTime(timezone=True)),
        sa.Column('since', sa.DateTime(timezone=True), nullable=False),
        sa.Column('prompt_sent', sa.Boolean(), nullable=False),
        sa.Column('token', sa.String(64)),
        sa.Column('locked_until', sa.DateTime(timezone=True)),
        sa.Column('retry_at', sa.DateTime(timezone=True)),
        sa.Column('payload', sa.JSON()),
        sa.Column('cutoff', sa.DateTime(timezone=True)),
        sa.Column('sent_parts', sa.Integer(), nullable=False))
    op.create_index('ix_daily_reports_next_at', 'daily_reports', ['next_at'])


def downgrade():
    op.drop_table('daily_reports')
