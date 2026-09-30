"""Initial schema, quotas and transactional notification outbox."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('users',
    sa.Column('telegram_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(length=128), nullable=True),
    sa.Column('first_name', sa.String(length=256), nullable=True),
    sa.Column('is_admin', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('manual_quota_day', sa.DateTime(timezone=True), nullable=True),
    sa.Column('manual_checks_today', sa.Integer(), nullable=False),
    sa.Column('last_manual_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('telegram_id')
    )
    op.create_table('stories',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('title', sa.String(length=160), nullable=False),
    sa.Column('original_input', sa.Text(), nullable=False),
    sa.Column('original_url', sa.Text(), nullable=True),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('current_state', sa.Text(), nullable=False),
    sa.Column('entities', sa.JSON(), nullable=False),
    sa.Column('keywords', sa.JSON(), nullable=False),
    sa.Column('search_queries', sa.JSON(), nullable=False),
    sa.Column('watch_goals', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('check_frequency_hours', sa.Integer(), nullable=False),
    sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_meaningful_update_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lock_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('lock_token', sa.String(length=64), nullable=True),
    sa.Column('last_manual_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('draft','active','paused','deleted')", name='ck_story_status'),
    sa.CheckConstraint('check_frequency_hours > 0', name='ck_story_frequency'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_stories_due', 'stories', ['status', 'next_check_at'], unique=False)
    op.create_index('ix_stories_user_status', 'stories', ['user_id', 'status'], unique=False)
    op.create_table('sources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('story_id', sa.Integer(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('normalized_url', sa.Text(), nullable=False),
    sa.Column('domain', sa.String(length=253), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('published_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('content_excerpt', sa.Text(), nullable=False),
    sa.Column('search_query', sa.Text(), nullable=False),
    sa.Column('relevance_score', sa.Float(), nullable=False),
    sa.Column('is_duplicate', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['story_id'], ['stories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('story_id', 'normalized_url', name='uq_source_story_url')
    )
    op.create_index(op.f('ix_sources_story_id'), 'sources', ['story_id'], unique=False)
    op.create_table('story_updates',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('story_id', sa.Integer(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('new_facts', sa.JSON(), nullable=False),
    sa.Column('previous_state', sa.Text(), nullable=False),
    sa.Column('new_state', sa.Text(), nullable=False),
    sa.Column('importance_score', sa.Float(), nullable=False),
    sa.Column('confidence_score', sa.Float(), nullable=False),
    sa.Column('source_urls', sa.JSON(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('is_demo', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_attempts', sa.Integer(), nullable=False),
    sa.Column('delivery_locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('delivery_lock_token', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['story_id'], ['stories.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_story_updates_story_id'), 'story_updates', ['story_id'], unique=False)
    op.create_index('ix_updates_outbox', 'story_updates', ['notified_at', 'delivery_locked_until'], unique=False)
    op.create_table('usage_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=True),
    sa.Column('story_id', sa.Integer(), nullable=True),
    sa.Column('operation', sa.String(length=80), nullable=False),
    sa.Column('provider', sa.String(length=80), nullable=False),
    sa.Column('model', sa.String(length=160), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('estimated_cost', sa.Float(), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['story_id'], ['stories.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_usage_events_story_id'), 'usage_events', ['story_id'], unique=False)
    op.create_index(op.f('ix_usage_events_user_id'), 'usage_events', ['user_id'], unique=False)
    op.create_index('ix_usage_operation_created', 'usage_events', ['operation', 'created_at'], unique=False)
    op.create_table('feedback',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('update_id', sa.Integer(), nullable=False),
    sa.Column('story_id', sa.Integer(), nullable=False),
    sa.Column('feedback_type', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("feedback_type IN ('useful','not_useful')", name='ck_feedback_kind'),
    sa.ForeignKeyConstraint(['story_id'], ['stories.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['update_id'], ['story_updates.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ),
    sa.PrimaryKeyConstraint('user_id', 'update_id')
    )
    op.create_index(op.f('ix_feedback_story_id'), 'feedback', ['story_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_feedback_story_id'), table_name='feedback')
    op.drop_table('feedback')
    op.drop_index('ix_usage_operation_created', table_name='usage_events')
    op.drop_index(op.f('ix_usage_events_user_id'), table_name='usage_events')
    op.drop_index(op.f('ix_usage_events_story_id'), table_name='usage_events')
    op.drop_table('usage_events')
    op.drop_index('ix_updates_outbox', table_name='story_updates')
    op.drop_index(op.f('ix_story_updates_story_id'), table_name='story_updates')
    op.drop_table('story_updates')
    op.drop_index(op.f('ix_sources_story_id'), table_name='sources')
    op.drop_table('sources')
    op.drop_index('ix_stories_user_status', table_name='stories')
    op.drop_index('ix_stories_due', table_name='stories')
    op.drop_table('stories')
    op.drop_table('users')
