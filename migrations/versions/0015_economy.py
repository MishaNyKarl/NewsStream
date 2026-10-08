"""Pause ordinary users immediately on rollout at the owner's request."""
from alembic import op
import sqlalchemy as sa

revision = '0015_economy'
down_revision = '0014_report_controls'
branch_labels = None
depends_on = None


def upgrade():
    table = op.create_table('economy_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_by', sa.String(80), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_economy_singleton'))
    from datetime import datetime, timezone
    op.bulk_insert(table, [{'id': 1, 'enabled': True, 'version': 1,
        'updated_by': 'deployment:owner-request', 'updated_at': datetime.now(timezone.utc)}])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'newswatch_admin') THEN
                GRANT SELECT, INSERT, UPDATE ON economy_state TO newswatch_admin;
            END IF;
        END $$"""))


def downgrade():
    op.drop_table('economy_state')
