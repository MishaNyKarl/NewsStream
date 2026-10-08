from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from app.economy import Economy
from app.errors import UserError
from app.models import User, Account, CommerceAudit, Story, utcnow
from app.service import BotService, usage_context
from test_repository import store as store, active
from test_admin import admin as admin, password_hash as password_hash, csrf, login
from test_bot import Harness
from test_worker import state
from app.worker import deliver_notifications


async def test_switch_durable_role_override_and_audit(store):
    repo, factory, settings = store
    await repo.admit_user(1, is_admin=True)
    await repo.admit_user(2)
    economy = Economy(factory)
    key = str(uuid4())
    await economy.save(True, 0, 'owner', key)
    await economy.save(True, 0, 'owner', key)
    assert await economy.allowed(1)
    assert not await economy.allowed(2)
    assert await economy.allowed(2, (2,))
    async with factory.begin() as db:
        db.add(Account(user_id=1, role='user'))
        db.add(Account(user_id=2, role='admin'))
    assert not await economy.allowed(1, (1,))
    assert await economy.allowed(2)
    with pytest.raises(UserError):
        await economy.save(False, 0, 'owner', str(uuid4()))
    await economy.save(False, 1, 'owner', str(uuid4()))
    assert await Economy(factory).allowed(1)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(CommerceAudit)) == 2


async def test_due_queue_skips_users_without_mutating_subscriptions(store):
    repo, factory, settings = store
    item = await active(repo)
    await Economy(factory).save(True, 0, 'owner', str(uuid4()))
    assert await repo.due_story_ids() == []
    assert await repo.claim_report_prompt() is None
    async with factory.begin() as db:
        assert (await db.get(Story, item.id)).status == 'active'
        (await db.get(User, 1)).is_admin = True
        (await db.get(Story, item.id)).next_check_at = utcnow()
    assert await repo.due_story_ids() == [item.id]
    await Economy(factory).save(False, 1, 'owner', str(uuid4()))
    assert await repo.due_story_ids() == [item.id]


@pytest.mark.parametrize('operation', ['llm_attempt', 'search'])
async def test_paid_boundary_blocks_existing_nonadmin_work(store, operation):
    repo, factory, settings = store
    await repo.admit_user(1)
    service = BotService(settings, repo, AsyncMock(), AsyncMock(), AsyncMock())
    await Economy(factory).save(True, 0, 'owner', str(uuid4()))
    token = usage_context.set({'user_id': 1})
    try:
        with pytest.raises(UserError, match='экономии'):
            await service._usage(operation)
    finally:
        usage_context.reset(token)


async def test_middleware_denies_even_old_buttons():
    h = Harness()
    h.service.economy_allowed = AsyncMock(return_value=False)
    await h.callback('full:12')
    assert 'экономии' in h.text
    h.service.repo.get_full_update.assert_not_called()


async def test_claimed_delivery_rechecks_policy_and_keeps_queue():
    service, bot, _, _ = state()
    service.economy_allowed = AsyncMock(return_value=False)
    await deliver_notifications(service, bot)
    bot.send_message.assert_not_called()
    service.repo.mark_notified.assert_not_called()


async def test_admin_confirm_theme_replay_and_resume(admin):
    client, *_ = admin
    await login(client)
    page = await client.get('/')
    assert 'Режим экономии: ВЫКЛ' in page.text
    values = {'csrf': csrf(page), 'enabled': '1', 'version': '0'}
    confirm = await client.post('/economy/prepare', data=values)
    assert 'Включить режим экономии?' in confirm.text
    assert 'class="economy"' not in (await client.get('/')).text
    import re
    values['nonce'] = re.search(r'name="nonce" value="([^"]+)"', confirm.text)[1]
    assert (await client.post('/economy', data=values)).status_code == 303
    page = await client.get('/calculator')
    assert 'class="economy"' in page.text and 'Режим экономии включён' in page.text
    assert (await client.post('/economy', data=values)).status_code == 409
    values = {'csrf': csrf(page), 'enabled': '0', 'version': '1'}
    confirm = await client.post('/economy/prepare', data=values)
    values['nonce'] = re.search(r'name="nonce" value="([^"]+)"', confirm.text)[1]
    assert (await client.post('/economy', data=values)).status_code == 303
    assert 'class="economy"' not in (await client.get('/')).text


@pytest.mark.parametrize('role', ['viewer', 'finance'])
async def test_read_only_roles_cannot_toggle(admin, password_hash, role):
    from test_admin import PASSWORD
    client, app, *_ = admin
    app.state.set_web_account('restricted', password_hash, role, True, 'owner')
    page = await client.get('/login')
    await client.post('/login', data={'csrf': csrf(page), 'username': 'restricted', 'password': PASSWORD})
    page = await client.get('/')
    assert 'economy-switch' not in page.text
    for path in ['/economy/prepare', '/economy']:
        assert (await client.post(path, data={'csrf': csrf(page), 'enabled': '1', 'version': '0'})).status_code == 403


async def test_economy_requires_csrf_and_origin(admin):
    client, *_ = admin
    await login(client)
    assert (await client.post('/economy/prepare', data={'enabled': '1', 'version': '0'})).status_code == 403
    page = await client.get('/')
    assert (await client.post('/economy/prepare', headers={'Origin': 'https://evil.test'},
        data={'csrf': csrf(page), 'enabled': '1', 'version': '0'})).status_code == 403
