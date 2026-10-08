"""Full reports are an explicit subscription feature, never a base entitlement."""
from alembic import op
import sqlalchemy as sa

revision = '0013_full_report_entitlement'
down_revision = '0012_account_settings'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('plans', sa.Column('full_reports', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute(sa.text('UPDATE plans SET full_reports = true WHERE price_minor > 0'))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'newswatch_admin') THEN
                GRANT UPDATE (full_reports) ON plans TO newswatch_admin;
            END IF;
        END $$"""))


def downgrade():
    op.drop_column('plans', 'full_reports')
