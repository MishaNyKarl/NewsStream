"""Tariff versions, entitlements and append-only credit accounting."""
from alembic import op
import sqlalchemy as sa
revision = '0008_commerce'
down_revision = '0007_product_analytics'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('plans',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('experiment', sa.String(80), nullable=False),
        sa.Column('price_minor', sa.Integer(), nullable=False),
        sa.Column('period_days', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('stories', sa.Integer(), nullable=False),
        sa.Column('manual_daily', sa.Integer(), nullable=False),
        sa.Column('llm_daily', sa.Integer(), nullable=False),
        sa.Column('intensive_slots', sa.Integer(), nullable=False),
        sa.Column('discussion', sa.Boolean(), nullable=False),
        sa.Column('discussion_credits', sa.Integer(), nullable=False),
        sa.Column('news_credits', sa.Integer(), nullable=False),
        sa.Column('check_credits', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('accounts',
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False, primary_key=True),
        sa.Column('balance', sa.BigInteger(), nullable=False),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('plans.id'), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('credit_exempt', sa.Boolean(), nullable=False),
        sa.Column('stories_override', sa.Integer(), nullable=True),
        sa.Column('manual_override', sa.Integer(), nullable=True),
        sa.Column('llm_override', sa.Integer(), nullable=True),
        sa.Column('intensive_override', sa.Integer(), nullable=True),
        sa.Column('discussion_override', sa.Boolean(), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.CheckConstraint('balance >= 0 AND balance <= 1000000000', name='ck_account_balance'),
    )
    op.create_table('credit_entries',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('key', sa.String(100), nullable=False, unique=True),
        sa.Column('delta', sa.BigInteger(), nullable=False),
        sa.Column('balance_after', sa.BigInteger(), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('actor', sa.String(80), nullable=False),
        sa.Column('reason', sa.String(240), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_credit_entries_user_id', 'credit_entries', ['user_id'])
    op.create_table('charges',
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('plans.id'), nullable=True),
        sa.Column('operation', sa.String(16), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_charges_user_id', 'charges', ['user_id'])
    op.create_table('topups',
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('plans.id'), nullable=True),
        sa.Column('id', sa.String(64), nullable=False, primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('credits', sa.Integer(), nullable=False),
        sa.Column('amount_minor', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('reference', sa.String(120), nullable=True, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_topups_user_id', 'topups', ['user_id'])
    op.create_table('commerce_audit',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True),
        sa.Column('key', sa.String(100), nullable=False, unique=True),
        sa.Column('actor', sa.String(80), nullable=False),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=True),
        sa.Column('action', sa.String(32), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.drop_index("uq_stories_user_intensive", table_name="stories")
    op.create_index("ix_stories_user_intensive", "stories", ["user_id"],
                    postgresql_where=sa.text("monitoring_mode = 'intensive' AND status IN ('active','paused')"),
                    sqlite_where=sa.text("monitoring_mode = 'intensive' AND status IN ('active','paused')"))
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(sa.text("""DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'newswatch_admin') THEN
                GRANT SELECT ON plans, accounts, credit_entries, charges, topups, commerce_audit TO newswatch_admin;
                GRANT INSERT ON plans, accounts, credit_entries, charges, topups, commerce_audit TO newswatch_admin;
                GRANT UPDATE ON accounts, charges, topups TO newswatch_admin;
                GRANT USAGE, SELECT ON SEQUENCE plans_id_seq, credit_entries_id_seq, commerce_audit_id_seq TO newswatch_admin;
            END IF;
        END $$"""))

def downgrade():
    op.drop_index("ix_stories_user_intensive", table_name="stories")
    op.create_index("uq_stories_user_intensive", "stories", ["user_id"], unique=True,
                    postgresql_where=sa.text("monitoring_mode = 'intensive' AND status IN ('active','paused')"),
                    sqlite_where=sa.text("monitoring_mode = 'intensive' AND status IN ('active','paused')"))
    op.drop_table('commerce_audit')
    op.drop_table('topups')
    op.drop_table('charges')
    op.drop_table('credit_entries')
    op.drop_table('accounts')
    op.drop_table('plans')
