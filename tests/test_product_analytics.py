import asyncio
import importlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.admin.data import Filters, MSK
from app.admin.product import analyse, billing, report
from app.ai import AIClient
from app.domain import StoryExtraction
from app.models import AnalyticsState, Base, ProductEvent, UsageEvent
from app.product_analytics import interaction_category
from test_pipeline import EXTRACTION, Response, Session, settings
from test_repository import active, store as store


def at(day, minute=0):
    return datetime(2026, 1, day, tzinfo=MSK)+timedelta(minutes=minute)


def users():
    return {1: {'created_at': at(1), 'is_admin': False},
            2: {'created_at': at(7), 'is_admin': False},
            3: {'created_at': at(1)-timedelta(days=1), 'is_admin': False}}


def events(*items):
    return [dict(id=i, user_id=uid, created_at=when, event=kind) for i, (uid, when, kind) in enumerate(items)]


def test_sessions_return_intervals_and_background_not_activity():
    data = events((1, at(1, 5), 'interaction_start'), (1, at(1, 15), 'interaction_menu'),
                  (1, at(1, 45), 'interaction_menu'), (2, at(7), 'notification_sent'))
    result = analyse(users(), data, at(1), at(9), at(1), now=at(9))
    assert result['active'] == 1 and result['actions'] == 3
    assert result['sessions_count'] == 2 and result['single_sessions'] == 1
    assert result['median_span'] == 600 and result['median_gap'] == 1800
    assert result['notifications'] == 1
    assert result['users'][1]['actions'] == 0


def test_retention_uses_mature_registration_cohorts_and_moscow_days():
    data = events((1, at(2), 'interaction_menu'), (1, at(8, 1439), 'interaction_menu'),
                  (2, at(8), 'interaction_menu'), (3, at(8), 'interaction_menu'))
    result = analyse(users(), data, at(1), at(9), at(1), now=at(9))
    assert result['new_users'] == 2
    assert result['retention'] == [
        {'day': 1, 'eligible': 2, 'returned': 2, 'percent': 100.0},
        {'day': 7, 'eligible': 1, 'returned': 1, 'percent': 100.0},
        {'day': 30, 'eligible': 0, 'returned': 0, 'percent': None}]
    partial = analyse(users(), data, at(1), at(9), at(1), now=at(8, 600))
    assert partial['retention'][1]['eligible'] == 0
    assert partial['dau'] == 2  # Future action at 23:59 is excluded.


def test_funnel_requires_order_and_no_backfill_before_coverage():
    data = events((1, at(1, 5), 'watch_started'), (1, at(1, 10), 'news_submitted'),
                  (1, at(1, 11), 'news_ready'), (1, at(1, 20), 'watch_started'),
                  (1, at(1, 25), 'notification_sent'), (1, at(1, 30), 'feedback_useful'),
                  (2, at(7, 5), 'watch_started'))
    result = analyse(users(), data, at(1), at(9), at(1), now=at(9))
    assert [r['users'] for r in result['funnel']] == [2, 1, 1, 1, 1, 1]
    assert result['activation_seconds'] == 1200
    later = analyse(users(), [], at(1), at(9), at(5), now=at(9))
    assert later['daily'][0]['users'] is None and later['new_users'] == 1


def test_cost_zero_unknown_estimate_never_conflated():
    base = dict(cost_source='unknown', currency=None, actual_cost=None, estimated_cost=0)
    rows = [base, base | {'cost_source': 'provider', 'currency': 'USD', 'actual_cost': 0},
            base | {'cost_source': 'estimate', 'currency': 'USD', 'estimated_cost': .2},
            base | {'estimated_cost': .3}]
    result = billing(rows)
    assert result['confirmed'] == Decimal(0) and result['estimated'] == Decimal('.2')
    assert result['unknown_calls'] == 2 and result['legacy'] == Decimal('.3')
    assert billing([])['confirmed'] is None
    assert billing([base])['legacy'] is None


@pytest.mark.parametrize('usage,provider,source,actual', [
    ({'cost': 0}, 'openrouter', 'provider', 0),
    ({'cost': .02}, 'openrouter', 'provider', .02),
    ({'cost': float('nan')}, 'openrouter', 'unknown', None),
    ({'cost': True}, 'openrouter', 'unknown', None),
    ({'cost': .02}, 'openai', 'unknown', None),
    ({'cost': .02, 'currency': 'USD'}, 'openai', 'provider', .02),
    ({'prompt_tokens': 100}, 'openrouter', 'estimate', None),
    ({}, 'openrouter', 'unknown', None),
])
async def test_provider_cost_provenance(usage, provider, source, actual):
    callback = AsyncMock()
    client = AIClient(settings(llm_provider=provider), callback)
    client._session = Session([Response({'choices': [{'message': {'content': json.dumps(EXTRACTION)},
                                                        'finish_reason': 'stop'}], 'usage': usage})])
    await client._request([], StoryExtraction, 'extract', 0)
    saved = callback.await_args.kwargs
    assert saved['cost_source'] == source and saved['actual_cost'] == actual
    assert saved['currency'] == ('USD' if source != 'unknown' else None)


async def test_event_deduplication_and_no_content_recording(store):
    repo, factory, _ = store
    await repo.admit_user(1)
    results = await asyncio.gather(*(repo.record_product_event('interaction_start', 1, 'message:secret')
                                     for _ in range(4)))
    assert results.count(True) == 1
    assert not await repo.record_product_event('interaction_start', 999, 'unknown')
    with pytest.raises(ValueError):
        await repo.record_product_event('arbitrary-secret', 1)
    async with factory() as session:
        rows = (await session.scalars(select(ProductEvent).where(ProductEvent.event == 'interaction_start'))).all()
        assert len(rows) == 1 and len(rows[0].dedupe_key) == 64 and 'secret' not in rows[0].dedupe_key
    assert interaction_category(SimpleNamespace(text='/start PRIVATE_INVITE')) == 'start'
    assert interaction_category(SimpleNamespace(data='useful:SECRET')) == 'feedback'
    assert interaction_category(SimpleNamespace(text='PRIVATE TEXT')) == 'input'


async def test_pause_causes_and_delete_keep_only_coarse_history(store):
    repo, factory, _ = store
    story = await active(repo)
    await repo.set_status(1, story.id, 'paused', reason='delivery_forbidden')
    await repo.set_status(1, story.id, 'active')
    await repo.set_status(1, story.id, 'paused')
    await repo.set_status(1, story.id, 'deleted')
    async with factory() as session:
        kinds = list(await session.scalars(select(ProductEvent.event)))
    assert kinds.count('watch_paused_delivery') == kinds.count('watch_paused') == 1
    assert kinds.count('watch_deleted') == 1


async def test_report_excludes_admins_and_attributes_only_selected_costs(store):
    repo, factory, _ = store
    await repo.admit_user(1)
    await repo.admit_user(2, is_admin=True)
    async with factory() as session:
        session.add(AnalyticsState(id=1, started_at=at(1)))
        from app.models import User
        for uid in (1, 2):
            user = await session.get(User, uid)
            user.created_at = at(1)
            session.add(ProductEvent(user_id=uid, event='interaction_start', created_at=at(2)))
            session.add(UsageEvent(user_id=uid, operation='llm', provider='openrouter', model='test',
                cost_source='provider', currency='USD', actual_cost=uid, request_id=f'request{uid}', created_at=at(2)))
        session.add(UsageEvent(operation='llm', estimated_cost=5, created_at=at(2)))
        await session.commit()
    data = SimpleNamespace(sessions=factory)
    filters = Filters(at(1), at(3), None, 1)
    result = await report(data, filters)
    assert result['active'] == 1 and result['billing']['confirmed'] == 1
    assert result['unattributed_calls'] == 1 and len(result['requests']) == 1
    assert (await report(data, filters, True))['billing']['confirmed'] == 3
    filters.user = 2
    assert (await report(data, filters))['users'] == []


async def test_migration_preserves_legacy_costs_without_inventing_provenance(store):
    _, factory, _ = store
    module = importlib.import_module('migrations.versions.0007_product_analytics')
    def upgrade(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            connection.execute(text("INSERT INTO usage_events(operation, provider, estimated_cost, input_tokens, output_tokens, created_at) VALUES ('llm', '', .2, 10, 20, CURRENT_TIMESTAMP)"))
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(upgrade)
    async with factory() as session:
        row = await session.scalar(select(UsageEvent))
        assert row.estimated_cost == .2 and row.cost_source == 'unknown' and row.actual_cost is None
        assert await session.get(AnalyticsState, 1)

async def test_bot_tracks_only_authorized_interactions_and_no_raw_payload():
    from test_bot import Harness
    harness = Harness()
    harness.service.track_interaction = AsyncMock()
    await harness.message('/start tester_invite')
    harness.service.track_interaction.assert_awaited_once_with(100, 'start', 'message:1')
    denied = Harness(authorized=False)
    denied.service.track_interaction = AsyncMock()
    await denied.message('/start secret')
    denied.service.track_interaction.assert_not_awaited()


async def test_telegram_membership_records_block_without_visit():
    from aiogram.types import Chat, ChatMemberMember, ChatMemberBanned, ChatMemberUpdated, Update, User
    from test_bot import Harness, NOW
    harness = Harness()
    harness.service.track_membership = AsyncMock()
    harness.service.track_interaction = AsyncMock()
    bot_user = User(id=777, is_bot=True, first_name='Bot')
    update = Update(update_id=876, my_chat_member=ChatMemberUpdated(
        chat=Chat(id=100, type='private'), from_user=User(id=100, is_bot=False, first_name='Tester'), date=NOW,
        old_chat_member=ChatMemberMember(user=bot_user),
        new_chat_member=ChatMemberBanned(user=bot_user, until_date=0)))
    await harness.dispatcher.feed_update(harness.bot, update)
    harness.service.track_membership.assert_awaited_once_with(100, True, 'membership:876')
    harness.service.track_interaction.assert_not_awaited()
    harness.service.authorize.assert_not_awaited()
    assert 'my_chat_member' in harness.dispatcher.resolve_used_update_types()
