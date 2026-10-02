import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models import Base, UserProfile
from app.profiles import refresh_profile
from test_repository import store as store


async def test_profile_cache_clears_unavailable_photo_and_preserves_activity(store):
    repo, factory, _ = store
    user = await repo.admit_user(1, first_name='Old')
    bot = AsyncMock()
    bot.get_chat.return_value = SimpleNamespace(first_name='Alice', last_name='Example', username='alice')
    bot.get_user_profile_photos.return_value = SimpleNamespace(photos=[[SimpleNamespace(file_id='test-file', file_size=10, width=100, height=100)]])
    async def download(file, destination, timeout):
        destination.write(b'\xff\xd8\xffphoto')
    bot.download.side_effect = download
    await refresh_profile(bot, factory, 1)
    async with factory() as session:
        row = await session.get(UserProfile, 1)
        assert row.display_name == 'Alice Example' and row.avatar == b'\xff\xd8\xffphoto'
    assert (await repo.get_user(1)).last_seen_at == user.last_seen_at
    bot.get_user_profile_photos.return_value = SimpleNamespace(photos=[])
    await refresh_profile(bot, factory, 1)
    async with factory() as session:
        assert (await session.get(UserProfile, 1)).avatar is None
    bot.get_chat.side_effect = RuntimeError('hidden-secret')
    await refresh_profile(bot, factory, 1)
    async with factory() as session:
        assert (await session.get(UserProfile, 1)).checked_at


async def test_profile_migration_matches_metadata(store):
    _, factory, _ = store
    module = importlib.import_module('migrations.versions.0009_user_profiles')
    def migrate(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(migrate)
