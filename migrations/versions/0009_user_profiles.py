"""Private cached Telegram profile thumbnails."""
from alembic import op
import sqlalchemy as sa

revision = '0009_user_profiles'
down_revision = '0008_commerce'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('user_profiles',
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), primary_key=True),
        sa.Column('display_name', sa.String(512)), sa.Column('username', sa.String(128)),
        sa.Column('avatar', sa.LargeBinary()), sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='newswatch_admin') THEN
                GRANT SELECT ON user_profiles TO newswatch_admin;
            END IF;
        END $$"""))


def downgrade():
    op.drop_table('user_profiles')
