"""Keep Telegram reply anchors and index the delivered notification journal."""
from alembic import op
import sqlalchemy as sa

revision = '0005_notification_journal'
down_revision = '0004_search_context'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('story_updates', sa.Column('telegram_message_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_updates_journal', 'story_updates', ['story_id', 'notified_at', 'id'])


def downgrade():
    op.drop_index('ix_updates_journal', table_name='story_updates')
    op.drop_column('story_updates', 'telegram_message_id')
