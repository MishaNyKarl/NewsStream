"""Keep submitted news independently of observations and durable ready notices."""
from alembic import op
import sqlalchemy as sa

revision = '0006_user_news'
down_revision = '0005_notification_journal'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('user_news',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('original_text', sa.Text(), nullable=False),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('use_text', sa.Boolean(), nullable=False),
        sa.Column('input_message_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('parsed_data', sa.JSON(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('story_id', sa.Integer(), sa.ForeignKey('stories.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('processing_token', sa.String(64), nullable=True),
        sa.Column('processing_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notice_suppressed', sa.Boolean(), nullable=False),
        sa.Column('notice_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notice_attempts', sa.Integer(), nullable=False),
        sa.Column('notice_token', sa.String(64), nullable=True),
        sa.Column('notice_locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('user_id', 'input_message_id', name='uq_news_input_message'),
        sa.CheckConstraint("status IN ('pending','processing','ready','failed')", name='ck_user_news_status'))
    op.create_index('ix_user_news_owner_page', 'user_news', ['user_id', 'id'])
    op.create_index('ix_user_news_story_id', 'user_news', ['story_id'])
    op.create_index('ix_user_news_ready_notice', 'user_news', ['status','notice_sent_at','notice_locked_until'])
    op.execute("""INSERT INTO user_news
        (user_id, original_text, source_url, use_text, status, parsed_data, story_id,
         created_at, updated_at, notice_suppressed, notice_attempts)
        SELECT user_id, original_input, original_url, true, 'ready',
         json_build_object('title',title,'short_summary',summary,'current_state',summary,
          'entities',entities,'keywords',keywords,'search_queries',search_queries,'watch_goals',watch_goals),
         id, created_at, updated_at, true, 0 FROM stories
         WHERE status != 'deleted' AND original_input != '' ORDER BY created_at, id""")


def downgrade():
    op.drop_table('user_news')
