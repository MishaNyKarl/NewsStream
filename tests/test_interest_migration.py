import importlib

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from app.models import Base
from test_repository import active, store as store


async def test_interest_migration_preserves_existing_stories_and_matches_models(store):
    repo, factory, _ = store
    item = await active(repo)
    module = importlib.import_module('migrations.versions.0003_user_interests')

    def upgrade_from_previous_schema(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            # The fixture supplies the current schema; remove only the new,
            # empty table to reproduce the previous release's schema.
            module.downgrade()
            assert 'user_interests' not in inspect(connection).get_table_names()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []

    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(upgrade_from_previous_schema)
    current = await repo.get_story(1, item.id)
    assert current.title == item.title and current.user_id == item.user_id
    assert current.status == item.status and current.next_check_at == item.next_check_at
    assert await repo.list_interests(1) == []
    saved = await repo.save_interest(1, item.id)
    assert saved.interest.title == item.title
