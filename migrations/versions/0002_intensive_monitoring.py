"""Persistent monitoring cadence and one intensive topic per user."""
from alembic import op
import sqlalchemy as sa

revision = "0002_intensive_monitoring"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("stories", sa.Column("monitoring_mode", sa.String(16), nullable=False, server_default="daily"))
    op.add_column("stories", sa.Column("intensive_started_at", sa.DateTime(timezone=True)))
    op.add_column("stories", sa.Column("intensive_until", sa.DateTime(timezone=True)))
    op.create_check_constraint("ck_story_monitoring_mode", "stories", "monitoring_mode IN ('daily','intensive')")
    op.create_check_constraint("ck_story_intensive_dates", "stories",
                               "monitoring_mode != 'intensive' OR (intensive_started_at IS NOT NULL AND intensive_until IS NOT NULL)")
    op.create_index("uq_stories_user_intensive", "stories", ["user_id"], unique=True,
                    postgresql_where=sa.text("monitoring_mode = 'intensive' AND status IN ('active','paused')"))


def downgrade():
    op.drop_index("uq_stories_user_intensive", table_name="stories")
    op.drop_constraint("ck_story_intensive_dates", "stories", type_="check")
    op.drop_constraint("ck_story_monitoring_mode", "stories", type_="check")
    for column in ("intensive_until", "intensive_started_at", "monitoring_mode"):
        op.drop_column("stories", column)
