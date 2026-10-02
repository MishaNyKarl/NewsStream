import asyncio
import importlib
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select

from app.commerce import Commerce
from app.domain import UserError
from app.models import Account, Base, Charge, CommerceAudit, CreditEntry, Plan, TopUp, utcnow
from test_repository import active, extraction, store as store


def key():
    return str(uuid4())


def plan(**changes):
    return dict(name='Premium A', experiment='A', price_minor='99000', period_days='30',
                stories='5', manual_daily='2', llm_daily='10', intensive_slots='2', discussion='1',
                news_credits='3', check_credits='2', discussion_credits='1', reason='Test hypothesis') | changes


async def configured(store):
    repo, factory, settings = store
    await repo.admit_user(1)
    commerce = Commerce(factory, settings)
    await commerce.command('plan', plan(), 'owner', key())
    await commerce.command('account', dict(user_id='1', plan_id='1', role='user', version='0',
                                         reason='Assign test plan'), 'owner', key())
    await commerce.command('grant', dict(user_id='1', amount='10', reason='Test credits'), 'owner', key())
    return repo, commerce, factory


async def test_reserve_concurrency_no_overdraft_and_idempotent_refund(store):
    _, commerce, factory = await configured(store)
    outcomes = await asyncio.gather(*(commerce.reserve(1, 'news', uuid4().hex) for _ in range(8)),
                                    return_exceptions=True)
    charges = [v for v in outcomes if isinstance(v, str)]
    assert len(charges) == 3 and sum(isinstance(v, UserError) for v in outcomes) == 5
    assert (await commerce.snapshot(1))['account'].balance == 1
    await asyncio.gather(*(commerce.settle(1, charges[0], False) for _ in range(4)))
    assert (await commerce.snapshot(1))['account'].balance == 4
    async with factory() as session:
        assert await session.scalar(select(func.sum(CreditEntry.delta))) == 4
        assert await session.scalar(select(func.count()).select_from(CreditEntry).where(CreditEntry.kind == 'refund')) == 1


async def test_paid_order_once_and_bonus_not_payment(store):
    _, commerce, factory = await configured(store)
    order_id = key()
    await commerce.command('topup', dict(user_id='1', credits='100', amount_minor='49000', reason='Purchase'), 'owner', order_id)
    assert (await commerce.snapshot(1))['account'].balance == 10
    payload = dict(user_id='1', order=order_id, reference='bank-confirmed-001', reason='Receipt verified')
    action_id = key()
    await asyncio.gather(*(commerce.command('paid', payload, 'owner', action_id) for _ in range(4)))
    assert (await commerce.snapshot(1))['account'].balance == 110
    with pytest.raises(UserError):
        await commerce.command('paid', payload, 'owner', key())
    async with factory() as session:
        order = await session.get(TopUp, order_id)
        assert order.status == 'manual_paid' and order.plan_id == 1
        assert await session.scalar(select(func.count()).select_from(TopUp)) == 1


async def test_stale_balance_and_negative_rejected_without_partial_audit(store):
    _, commerce, factory = await configured(store)
    with pytest.raises(UserError):
        await commerce.command('set_balance', dict(user_id='1', amount='50', version='0', reason='Stale page'), 'owner', key())
    with pytest.raises(UserError):
        await commerce.command('grant', dict(user_id='1', amount='-11', reason='Overdraft'), 'owner', key())
    assert (await commerce.snapshot(1))['account'].balance == 10
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CommerceAudit)) == 3


async def test_expiry_overrides_and_admin_revocation(store):
    _, commerce, factory = await configured(store)
    assert not await commerce.role(1, inherited=True)
    async with factory.begin() as session:
        a = await session.get(Account, 1)
        a.expires_at = utcnow()-timedelta(seconds=1)
    p = (await commerce.snapshot(1))['limits']
    assert p['plan_id'] is None and p['news_credits'] == 0 and p['intensive_slots'] == 1
    a = (await commerce.snapshot(1))['account']
    await commerce.command('account', dict(user_id='1', version=str(a.version), plan_id='1', days='5', role='admin',
        stories_override='1', intensive_override='3', credit_exempt='1', reason='Personal privilege'), 'owner', key())
    assert await commerce.role(1, inherited=False)
    p = (await commerce.snapshot(1))['limits']
    assert p['stories'] == 1 and p['intensive_slots'] == 3 and p['check_credits'] == 0


async def test_tariff_limits_enforced_and_two_intensive_slots(store):
    repo, commerce, _ = await configured(store)
    a, b, c = await active(repo), await active(repo), await active(repo)
    await repo.set_monitoring_mode(1, a.id, 'intensive')
    await repo.set_monitoring_mode(1, b.id, 'intensive')
    with pytest.raises(UserError):
        await repo.set_monitoring_mode(1, c.id, 'intensive')
    account = (await commerce.snapshot(1))['account']
    await commerce.command('account', dict(user_id='1', version=str(account.version), plan_id='1', role='user',
        stories_override='0', manual_override='0', reason='Limit test'), 'owner', key())
    with pytest.raises(UserError):
        await repo.create_draft(1, 'Test story', None, extraction())
    with pytest.raises(UserError):
        await repo.claim_story(a.id, user_id=1, manual=True)


async def test_tariffs_immutable_and_price_snapshot(store):
    _, commerce, factory = await configured(store)
    charge = await commerce.reserve(1, 'news', uuid4().hex)
    await commerce.command('plan', plan(news_credits='20'), 'owner', key())
    await commerce.settle(1, charge, True)
    async with factory() as session:
        assert (await session.get(Charge, charge)).amount == 3
        assert (await session.get(Plan, 1)).news_credits == 3
        assert (await session.get(Plan, 2)).news_credits == 20
    assert (await commerce.snapshot(1))['account'].balance == 7


async def test_orphan_refund_waits_and_cannot_repeat(store):
    _, commerce, factory = await configured(store)
    charge = await commerce.reserve(1, 'news', uuid4().hex)
    payload = dict(user_id='1', charge=charge, reason='Process stopped')
    with pytest.raises(UserError):
        await commerce.command('refund_stale', payload, 'owner', key())
    async with factory.begin() as session:
        (await session.get(Charge, charge)).created_at = utcnow()-timedelta(hours=1)
    await commerce.command('refund_stale', payload, 'owner', key())
    await commerce.settle(1, charge, False)
    assert (await commerce.snapshot(1))['account'].balance == 10


async def test_schema_migration_matches_and_preserves_users(store):
    repo, factory, _ = store
    await active(repo)
    migration = importlib.import_module('migrations.versions.0008_commerce')
    def upgrade(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as conn:
        await conn.run_sync(upgrade)
    assert await repo.get_user(1)

async def test_service_success_and_failure_settle_credits(store):
    from unittest.mock import AsyncMock
    from app.service import BotService
    repo, commerce, factory = await configured(store)
    repo.settings.llm_provider = 'openrouter'
    repo.settings.llm_api_key = 'test-key'
    repo.settings.llm_model = 'test-model'
    ai = AsyncMock()
    ai.extract.return_value = extraction()
    service = BotService(repo.settings, repo=repo, ai=ai)
    story = await service.prepare_story(1, 'Следить за открытием новой станции')
    assert (await commerce.snapshot(1))['account'].balance == 7
    ai.extract.side_effect = RuntimeError('failure')
    with pytest.raises(UserError):
        await service.prepare_story(1, 'Следить за другой новой станцией')
    assert (await commerce.snapshot(1))['account'].balance == 7
    ai.discuss.return_value = 'Подтверждённой даты пока нет.'
    assert await service.discuss(1, story.id, 'Когда открытие?') == 'Подтверждённой даты пока нет.'
    assert (await commerce.snapshot(1))['account'].balance == 6
    with pytest.raises(UserError):
        await service.discuss(2, story.id, 'Чужая тема?')
    await service.ai.close()


async def test_paid_subscription_edit_preserves_expiry_and_limits_news_path(store):
    repo, commerce, _ = await configured(store)
    before = (await commerce.snapshot(1))['account']
    await commerce.command('account', dict(user_id='1', version=str(before.version), plan_id='1', role='user',
        stories_override='0', reason='Block new watches'), 'owner', key())
    assert (await commerce.snapshot(1))['account'].expires_at == before.expires_at
    item = await repo.save_user_news(1, 'Новая новость для проверки', input_message_id=88)
    claimed = await repo.claim_user_news(1, item.id)
    await repo.finish_user_news(1, item.id, claimed.processing_token, extraction())
    with pytest.raises(UserError):
        await repo.user_news_story(1, item.id)
