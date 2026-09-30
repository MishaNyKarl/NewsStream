"""Distinguish recovered context from newly reported developments."""
from alembic import op
import sqlalchemy as sa

revision = '0004_search_context'
down_revision = '0003_user_interests'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('story_updates', sa.Column('update_kind', sa.String(16), nullable=False, server_default='development'))
    op.create_check_constraint('ck_update_kind', 'story_updates', "update_kind IN ('development','context')")


def downgrade():
    op.drop_constraint('ck_update_kind', 'story_updates', type_='check')
    op.drop_column('story_updates', 'update_kind')
