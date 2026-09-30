"""Explicit user topic interests, separate from monitoring."""
from alembic import op
import sqlalchemy as sa

revision = "0003_user_interests"
down_revision = "0002_intensive_monitoring"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("user_interests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_story_id", sa.Integer(), sa.ForeignKey("stories.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("entities", sa.JSON(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "source_story_id", name="uq_interest_user_story"), sqlite_autoincrement=True)
    op.create_index("ix_interests_user_id", "user_interests", ["user_id", "id"])


def downgrade():
    op.drop_index("ix_interests_user_id", table_name="user_interests")
    op.drop_table("user_interests")
