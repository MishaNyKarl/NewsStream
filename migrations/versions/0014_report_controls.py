"""Operator experiments with bounded search and report sizes."""
from alembic import op
import sqlalchemy as sa

revision = '0014_report_controls'
down_revision = '0013_full_report_entitlement'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('report_controls',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('values', sa.JSON(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('updated_by', sa.String(80), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('id = 1', name='ck_report_controls_singleton'))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'newswatch_admin') THEN
                GRANT SELECT, INSERT, UPDATE ON report_controls TO newswatch_admin;
            END IF;
        END $$"""))


def downgrade():
    op.drop_table('report_controls')
