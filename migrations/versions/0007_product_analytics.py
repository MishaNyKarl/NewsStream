"""Append-only coarse product analytics and explicit LLM billing provenance."""
from alembic import op
import sqlalchemy as sa

revision = '0007_product_analytics'
down_revision = '0006_user_news'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('usage_events', sa.Column('cost_source', sa.String(16), nullable=False, server_default='unknown'))
    op.add_column('usage_events', sa.Column('currency', sa.String(8)))
    op.add_column('usage_events', sa.Column('actual_cost', sa.Float()))
    op.add_column('usage_events', sa.Column('request_id', sa.String(64)))
    op.create_index('ix_usage_events_request_id', 'usage_events', ['request_id'])
    op.create_table('product_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='SET NULL')),
        sa.Column('event', sa.String(64), nullable=False),
        sa.Column('dedupe_key', sa.String(64), unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_product_user_time', 'product_events', ['user_id', 'created_at'])
    op.create_index('ix_product_event_time', 'product_events', ['event', 'created_at'])
    op.create_table('analytics_state', sa.Column('id', sa.Integer(), primary_key=True),
                    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False))
    # Honest coverage marker: do not invent activity before installation.
    op.execute(sa.text('INSERT INTO analytics_state(id, started_at) VALUES (1, CURRENT_TIMESTAMP)'))
    # The production admin uses a separate read-only role. New tables do not
    # inherit its existing grants; include this in the automated release.
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'newswatch_admin') THEN
                GRANT SELECT ON TABLE product_events, analytics_state TO newswatch_admin;
            END IF;
        END $$"""))


def downgrade():
    op.drop_table('product_events')
    op.drop_table('analytics_state')
    op.drop_index('ix_usage_events_request_id', table_name='usage_events')
    for name in ('request_id', 'actual_cost', 'currency', 'cost_source'):
        op.drop_column('usage_events', name)
