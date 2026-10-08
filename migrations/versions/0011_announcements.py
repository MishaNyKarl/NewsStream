"""Durable operator announcements with narrowly scoped admin grants."""
from alembic import op
import sqlalchemy as sa

revision = '0011_announcements'
down_revision = '0010_daily_reports'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('announcements',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key', sa.String(100), nullable=False, unique=True),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('audience', sa.String(32), nullable=False),
        sa.Column('button', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_by', sa.String(80), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('launched_at', sa.DateTime(timezone=True)),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('draft','queued','completed','cancelled')", name='ck_announcement_status'))
    op.create_table('announcement_deliveries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('announcement_id', sa.Integer(), sa.ForeignKey('announcements.id', ondelete='CASCADE'), nullable=False),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('retry_at', sa.DateTime(timezone=True)),
        sa.Column('locked_until', sa.DateTime(timezone=True)),
        sa.Column('token', sa.String(64)),
        sa.Column('sent_at', sa.DateTime(timezone=True)),
        sa.Column('message_id', sa.BigInteger()),
        sa.Column('error', sa.String(64)),
        sa.UniqueConstraint('announcement_id', 'user_id', name='uq_announcement_recipient'),
        sa.CheckConstraint("status IN ('pending','sending','sent','blocked','failed','cancelled')", name='ck_announcement_delivery_status'))
    op.create_index('ix_announcement_delivery_due', 'announcement_deliveries', ['status', 'retry_at', 'locked_until'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='newswatch_admin') THEN
                GRANT SELECT ON daily_reports, announcements, announcement_deliveries TO newswatch_admin;
                GRANT INSERT, UPDATE ON announcements, announcement_deliveries TO newswatch_admin;
                GRANT USAGE, SELECT ON SEQUENCE announcements_id_seq, announcement_deliveries_id_seq TO newswatch_admin;
            END IF;
        END $$"""))


def downgrade():
    op.drop_table('announcement_deliveries')
    op.drop_table('announcements')
