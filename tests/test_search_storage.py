import importlib
from dataclasses import replace
from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.models import Base, StoryUpdate
from test_repository import active, analysis, candidate, store as store


@pytest.mark.parametrize('hours,kind', [(-2, 'context'), (1, 'development'), (None, 'context')])
async def test_pre_subscription_evidence_is_stored_as_context_not_new_event(store, hours, kind):
    repo, _, _ = store
    item = await active(repo)
    claim = await repo.claim_story(item.id)
    await repo.set_monitoring_mode(item.user_id, item.id, 'intensive')
    source = replace(candidate(), published_at=item.created_at + timedelta(hours=hours) if hours is not None else None)
    update = await repo.save_check(item.id, claim.lock_token, [source], analysis())
    assert update.update_kind == kind
    assert update.previous_state == item.current_state
    assert update.new_state == analysis().updated_state
    assert len(await repo.pending_notifications()) == 1
    assert await repo.save_check(item.id, claim.lock_token, [source], analysis()) is None


async def test_changed_article_same_url_updates_source_and_emits_once(store):
    repo, _, _ = store
    item = await active(repo)
    claim = await repo.claim_story(item.id)
    await repo.set_monitoring_mode(item.user_id, item.id, 'intensive')
    original = candidate()
    assert await repo.save_check(item.id, claim.lock_token, [original], analysis(False)) is None
    revised = replace(original, content_hash='updated-body', content_excerpt='Confirmed landing. All passengers safe.')
    assert await repo.save_check(item.id, 'stale-token', [revised], analysis()) is None
    assert (await repo.known_sources(item.id))[0].content_hash == original.content_hash
    update = await repo.save_check(item.id, claim.lock_token, [revised], analysis())
    assert update is not None
    sources = await repo.known_sources(item.id)
    assert len(sources) == 1 and sources[0].content_hash == revised.content_hash
    assert await repo.save_check(item.id, claim.lock_token, [revised], analysis()) is None
    assert len(await repo.pending_notifications()) == 1
    fragment = replace(revised, content_hash='rss-only', content_excerpt='Headline', full_text=False)
    assert await repo.save_check(item.id, claim.lock_token, [fragment], analysis()) is None
    assert (await repo.known_sources(item.id))[0].content_hash == revised.content_hash


async def test_search_migration_preserves_old_updates_and_matches_models(store):
    repo, factory, _ = store
    if factory.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Production ALTER constraints require PostgreSQL')
    item = await active(repo)
    claim = await repo.claim_story(item.id)
    update = await repo.save_check(item.id, claim.lock_token, [candidate()], analysis())
    module = importlib.import_module('migrations.versions.0004_search_context')
    def upgrade(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(upgrade)
    async with factory() as session:
        current = await session.scalar(select(StoryUpdate).where(StoryUpdate.id == update.id))
        assert current.update_kind == 'development' and current.new_facts == update.new_facts
    assert (await repo.get_story(1, item.id)).current_state == update.new_state
