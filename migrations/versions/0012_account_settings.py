"""Account promotion preferences, preserving existing entitlements."""
from alembic import op
import sqlalchemy as sa

revision = '0012_account_settings'
down_revision = '0011_announcements'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('accounts', sa.Column('promotions_enabled', sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade():
    op.drop_column('accounts', 'promotions_enabled')
