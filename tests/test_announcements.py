import asyncio
import importlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.announcements import Announcements, UPDATE_TEMPLATE
from app.errors import UserError
from app.models import Announcement, AnnouncementDelivery, Base, User, utcnow
from app.worker import deliver_announcements
from test_admin import PASSWORD, admin as admin, csrf, login, password_hash as password_hash
from test_repository import active, store as store


def values(**changes):
    return dict(title='Обновление <бота>', body='Ваши новости сохранены. & 😀', audience='all', button='report') | changes


async def prepared(store, **changes):
    repo, factory, _ = store
    await active(repo, 1)
    await active(repo, 2)
    queue = Announcements(factory)
    campaign = await queue.create(values(**changes), 'owner', 'campaign-001')
    return queue, campaign


async def test_snapshot_preview_launch_and_create_are_idempotent(store):
    queue, campaign = await prepared(store)
    repo, factory, _ = store
    detail = await queue.detail(campaign.id)
    assert detail['total'] == 2 and detail['counts'] == {'pending': 2}
    assert await queue.claim() is None  # Draft is never sent.
    assert (await queue.create(values(body='different'), 'owner', 'campaign-001')).id == campaign.id
    async with factory.begin() as session:
        session.add(User(telegram_id=3))
    assert (await queue.detail(campaign.id))['total'] == 2
    await asyncio.gather(queue.action(campaign.id, 'launch'), queue.action(campaign.id, 'launch'))
    claims = await asyncio.gather(queue.claim(), Announcements(factory).claim())
    assert sum(c is not None for c in claims) == 1
    first, _ = next(c for c in claims if c is not None)
    assert not await queue.finish(first.id, 'wrong', 'sent')
    assert await queue.finish(first.id, first.token, 'sent', message_id=123)
    second, _ = await queue.claim()
    assert second.user_id != first.user_id
    await queue.finish(second.id, second.token, 'sent', message_id=124)
    assert await queue.claim() is None
    assert (await queue.detail(campaign.id))['campaign'].status == 'completed'
    assert (await queue.detail(campaign.id))['counts'] == {'sent': 2}


async def test_audiences_include_paused_expired_and_exclude_intensive_and_configured(store):
    repo, factory, _ = store
    ordinary = await active(repo, 1)
    intensive = await active(repo, 2)
    await repo.set_monitoring_mode(2, intensive.id, 'intensive')
    await repo.set_status(1, ordinary.id, 'paused')
    queue = Announcements(factory)
    async with queue.transaction() as session:
        assert await queue.recipients(session, 'daily') == [1]
        assert await queue.recipients(session, 'active') == [2]
        assert await queue.recipients(session, 'no_time') == [1]
    await repo.set_report_time(1, '00:00')
    async with queue.transaction() as session:
        assert await queue.recipients(session, 'no_time') == []
    from app.models import Story
    async with factory.begin() as session:
        (await session.get(Story, intensive.id)).intensive_until = utcnow() - timedelta(seconds=1)
    async with queue.transaction() as session:
        assert await queue.recipients(session, 'daily') == [1, 2]
        assert await queue.recipients(session, 'no_time') == [2]
        assert await queue.recipients(session, 'selected', '2, 1 2') == [1, 2]
        for bad in ('999', '0', '1,xyz', str(2**63), ''):
            with pytest.raises(UserError):
                await queue.recipients(session, 'selected', bad)


async def test_cancel_handles_inflight_delivery_and_expired_leases(store):
    queue, campaign = await prepared(store)
    await queue.action(campaign.id, 'launch')
    delivery, _ = await queue.claim()
    await queue.action(campaign.id, 'cancel')
    assert not await queue.may_send(delivery.id, delivery.token)
    # If Telegram already accepted the message its actual delivery is recorded.
    await queue.finish(delivery.id, delivery.token, 'sent', message_id=123)
    assert (await queue.detail(campaign.id))['counts'] == {'sent': 1, 'cancelled': 1}
    assert await queue.claim() is None
    with pytest.raises(UserError):
        await queue.action(campaign.id, 'retry')


async def test_expired_claim_is_recoverable_after_restart_and_stale_ack_is_rejected(store):
    queue, campaign = await prepared(store)
    _, factory, _ = store
    await queue.action(campaign.id, 'launch')
    first, _ = await queue.claim()
    async with factory.begin() as session:
        (await session.get(AnnouncementDelivery, first.id)).locked_until = utcnow() - timedelta(seconds=1)
    peer = Announcements(factory)
    second, _ = await peer.claim()
    assert second.id == first.id and second.token != first.token
    assert not await queue.finish(first.id, first.token, 'sent')
    await peer.action(campaign.id, 'cancel')
    async with factory.begin() as session:
        (await session.get(AnnouncementDelivery, second.id)).locked_until = utcnow() - timedelta(seconds=1)
    assert await peer.claim() is None
    assert (await peer.detail(campaign.id))['counts'] == {'cancelled': 2}


async def test_only_failed_delivery_is_retried_not_sent_or_blocked(store):
    queue, campaign = await prepared(store)
    await queue.action(campaign.id, 'launch')
    first, _ = await queue.claim()
    await queue.finish(first.id, first.token, 'blocked', error='bot_blocked')
    second, _ = await queue.claim()
    await queue.finish(second.id, second.token, 'permanent', error='telegram_rejected')
    assert (await queue.detail(campaign.id))['campaign'].status == 'completed'
    await queue.action(campaign.id, 'retry')
    retry, _ = await queue.claim()
    assert retry.id == second.id and retry.attempts == 1
    await queue.finish(retry.id, retry.token, 'sent', message_id=123)
    await queue.action(campaign.id, 'retry')
    assert await queue.claim() is None
    assert (await queue.detail(campaign.id))['counts'] == {'blocked': 1, 'sent': 1}


async def test_transient_failure_backoff_and_rate_limit_are_persistent_and_global(store):
    queue, campaign = await prepared(store)
    _, factory, _ = store
    await queue.action(campaign.id, 'launch')
    first, _ = await queue.claim()
    await queue.finish(first.id, first.token, 'retry', error='rate_limit', retry_after=120)
    assert await Announcements(factory).claim() is None  # Includes other recipients.
    async with factory.begin() as session:
        (await session.get(AnnouncementDelivery, first.id)).retry_at = utcnow() - timedelta(seconds=1)
    retry, _ = await Announcements(factory).claim()
    assert retry.id == first.id and retry.attempts == 2


async def test_transient_delivery_stops_after_five_attempts_and_expired_last_lease_is_terminal(store):
    queue, campaign = await prepared(store, audience='selected', ids='1')
    _, factory, _ = store
    await queue.action(campaign.id, 'launch')
    for attempt in range(1, 6):
        delivery, _ = await queue.claim()
        assert delivery.attempts == attempt
        if attempt == 5:
            # Lost worker, including its last permitted attempt.
            async with factory.begin() as session:
                (await session.get(AnnouncementDelivery, delivery.id)).locked_until = utcnow() - timedelta(seconds=1)
        else:
            await queue.finish(delivery.id, delivery.token, 'retry', error='TimeoutError')
            async with factory.begin() as session:
                (await session.get(AnnouncementDelivery, delivery.id)).retry_at = utcnow() - timedelta(seconds=1)
    assert await queue.claim() is None
    detail = await queue.detail(campaign.id)
    assert detail['counts'] == {'failed': 1}
    assert detail['campaign'].status == 'completed'
    await queue.action(campaign.id, 'retry')
    assert (await queue.claim())[0].attempts == 1


async def test_worker_sends_plain_text_and_buttons_and_marks_blocked_without_pausing(store):
    queue, campaign = await prepared(store)
    repo, _, _ = store
    await queue.action(campaign.id, 'launch')
    bot = AsyncMock()
    bot.send_message.side_effect = [SimpleNamespace(message_id=123),
        TelegramForbiddenError(method=SendMessage(chat_id=2, text='x'), message='Forbidden')]
    service = SimpleNamespace(repo=repo, _error=AsyncMock())
    await deliver_announcements(service, bot)
    assert bot.send_message.await_count == 2
    kwargs = bot.send_message.call_args_list[0].kwargs
    assert kwargs['parse_mode'] is None
    assert kwargs['reply_markup'].inline_keyboard[0][0].callback_data == 'report:settings'
    assert '<бота>' in bot.send_message.call_args_list[0].args[1]
    assert (await queue.detail(campaign.id))['counts'] == {'sent': 1, 'blocked': 1}
    assert all(story.status == 'active' for story in await repo.list_stories(2))


async def test_worker_rate_limit_stops_batch(store):
    queue, campaign = await prepared(store)
    repo, _, _ = store
    await queue.action(campaign.id, 'launch')
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramRetryAfter(method=SendMessage(chat_id=1, text='x'), message='retry', retry_after=120)
    await deliver_announcements(SimpleNamespace(repo=repo, _error=AsyncMock()), bot)
    assert bot.send_message.await_count == 1
    assert await queue.claim() is None


async def test_migration_matches_models_and_grants_are_scoped(store):
    _, factory, _ = store
    module = importlib.import_module('migrations.versions.0011_announcements')
    def migrate(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(migrate)


async def seed_admin(data):
    async with data.sessions.begin() as session:
        session.add_all([User(telegram_id=1, first_name='Alice <test>'), User(telegram_id=2)])


def fields(form):
    return {tag['name']: tag.get('value', '') for tag in form.select('input[name]')}


async def test_admin_preview_test_launch_results_and_duplicate_submission(admin):
    client, app, data, *_ = admin
    await seed_admin(data)
    await login(client)
    page = await client.get('/announcements')
    assert page.status_code == 200 and 'Ещё не выбрали время' in page.text
    payload = fields(BeautifulSoup(page.text, 'html.parser').select_one('#compose form')) | values()
    response = await client.post('/announcements', data=payload)
    assert response.status_code == 303
    assert (await client.post('/announcements', data=payload)).headers['location'] == response.headers['location']
    page = await client.get(response.headers['location'])
    assert 'Обновление &lt;бота&gt;' in page.text and 'Alice &lt;test&gt;' in page.text
    soup = BeautifulSoup(page.text, 'html.parser')
    test_payload = fields(soup.select_one('form:has(input[value="test"])')) | {'test_user': '1'}
    assert (await client.post('/announcements', data=test_payload)).status_code == 303
    async with data.sessions() as session:
        assert (await session.get(Announcement, 1)).status == 'draft'
        assert (await session.get(Announcement, 2)).status == 'queued'
        assert await session.scalar(select(func.count()).select_from(AnnouncementDelivery).where(
            AnnouncementDelivery.announcement_id == 2)) == 1
    launch = fields(soup.select_one('form:has(input[value="launch"])'))
    assert (await client.post('/announcements', data=launch | {'nonce': 'wrong'})).status_code == 400
    assert (await client.post('/announcements', data=launch)).status_code == 303
    assert (await client.post('/announcements', data=launch)).status_code == 303
    assert any(row['action'] == 'announcement_requested' for row in app.state.audit_rows(1))
    copied = await client.get('/announcements?copy=1')
    assert 'Обновление &lt;бота&gt;' in copied.text


@pytest.mark.parametrize('role', ['viewer', 'finance'])
async def test_announcement_write_permissions_and_csrf(admin, role, password_hash):
    client, app, data, *_ = admin
    await seed_admin(data)
    await login(client)
    page = await client.get('/announcements')
    token = csrf(page)
    assert (await client.post('/announcements', data=values() | {'csrf': 'bad', 'action': 'create', 'key': 'test-1234'})).status_code == 403
    app.state.set_web_account('readonly', password_hash, role, True, 'owner')
    await client.post('/logout', data={'csrf': token})
    page = await client.get('/login')
    assert (await client.post('/login', data={'csrf': csrf(page), 'username': 'readonly', 'password': PASSWORD})).status_code == 303
    response = await client.get('/announcements')
    assert response.status_code == 200 and 'Предпросмотр и получатели' not in response.text
    assert (await client.post('/announcements', data=values() | {'csrf': token, 'action': 'create', 'key': 'test-1234'})).status_code == 403


async def test_private_announcement_page_and_long_russian_form(admin):
    client, _, data, *_ = admin
    assert (await client.get('/announcements')).status_code == 303
    await seed_admin(data)
    await login(client)
    page = await client.get('/announcements')
    payload = fields(BeautifulSoup(page.text, 'html.parser').select_one('#compose form')) | values(body='я' * 2400)
    assert (await client.post('/announcements', data=payload)).status_code == 303
    payload['key'] = 'different-key'
    payload['body'] = 'я' * 2501
    assert (await client.post('/announcements', data=payload)).status_code == 400
    assert UPDATE_TEMPLATE['body']
