import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from aiogram.methods import EditMessageText, SendMessage
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.announcements import Announcements, TEMPLATES
from app.commerce import Commerce
from app.domain import UserError
from app.models import Account, Base, CommerceAudit, Story, UserInterest
from app.worker import deliver_announcements
from test_admin import admin as admin, login, password_hash as password_hash
from test_bot import Harness
from test_commerce import plan
from test_repository import active, analysis, candidate, extraction, store as store


async def test_reset_erases_owned_content_invalidates_work_and_preserves_billing(store):
    repo, factory, settings = store
    story = await active(repo)
    await active(repo, 2)
    news = await repo.save_user_news(1, 'Обрабатываемая новость', None, True, 12)
    processing = await repo.claim_user_news(1, news.id)
    lease = await repo.claim_story(story.id, user_id=1, manual=True)
    await repo.save_check(story.id, lease.lock_token, [candidate()], analysis())
    await repo.set_report_time(1, '09:00')
    async with factory.begin() as session:
        session.add(Account(user_id=1, balance=123, role='admin', promotions_enabled=False))
        session.add(UserInterest(user_id=1, title='Тема', summary='Текст', entities=[], keywords=[], input_fingerprint='a'))
    await repo.reset_content(1)
    assert not await repo.list_stories(1)
    assert len(await repo.list_stories(2)) == 1
    assert not await repo.list_user_news(1)
    assert not await repo.list_interests(1)
    assert await repo.report_preference(1) is None
    assert await repo.finish_check(story.id, lease.lock_token) is False
    with pytest.raises(UserError):
        await repo.finish_user_news(1, news.id, processing.processing_token, extraction())
    async with factory() as session:
        erased = await session.get(Story, story.id)
        assert erased.title == 'Удалено' and erased.current_state == '' and erased.lock_token is None
    account = (await Commerce(factory, settings).snapshot(1))['account']
    assert account.balance == 123 and account.role == 'admin' and not account.promotions_enabled
    await repo.reset_content(1)  # No resurrection and safe to repeat.


async def test_admin_self_plan_checks_current_role_and_never_records_payment(store):
    repo, factory, settings = store
    await repo.admit_user(1, is_admin=True)
    await repo.admit_user(2)
    commerce = Commerce(factory, settings)
    await commerce.command('plan', plan(), 'owner', str(uuid4()))
    key = 'self-plan-test-001'
    with pytest.raises(UserError):
        await commerce.admin_self_plan(2, 1, key)
    await commerce.admin_self_plan(1, 1, key)
    await commerce.admin_self_plan(1, 1, key)
    account = (await commerce.snapshot(1))['account']
    assert account.plan_id == 1 and account.balance == 0 and account.role == 'inherit'
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(CommerceAudit).where(CommerceAudit.key == key)) == 1
    await commerce.admin_self_plan(1, 0, 'self-plan-test-002')
    assert (await commerce.snapshot(1))['account'].plan_id is None
    async with factory.begin() as session:
        (await session.get(Account, 1)).role = 'user'
    with pytest.raises(UserError):
        await commerce.admin_self_plan(1, 1, 'self-plan-test-003')


async def test_promotion_opt_out_before_draft_and_after_launch(store):
    repo, factory, settings = store
    await active(repo, 1)
    await active(repo, 2)
    commerce, queue = Commerce(factory, settings), Announcements(factory)
    await commerce.set_promotions(1, False)
    ad = await queue.create(TEMPLATES['discount'], 'owner', 'discount-campaign-001')
    detail = await queue.detail(ad.id)
    assert detail['total'] == 1 and detail['recipients'][0][1].telegram_id == 2
    await queue.action(ad.id, 'launch')
    await commerce.set_promotions(2, False)
    bot = AsyncMock()
    await deliver_announcements(SimpleNamespace(repo=repo, commerce=commerce, _error=AsyncMock()), bot)
    bot.send_message.assert_not_awaited()
    assert (await queue.detail(ad.id))['counts'] == {'cancelled': 1}
    service = await queue.create(TEMPLATES['maintenance'], 'owner', 'service-campaign-001')
    assert (await queue.detail(service.id))['total'] == 2


async def test_ad_delivery_has_catalog_and_settings_buttons(store):
    repo, factory, settings = store
    await active(repo)
    queue = Announcements(factory)
    ad = await queue.create(TEMPLATES['offer'], 'owner', 'offer-campaign-001')
    await queue.action(ad.id, 'launch')
    bot = AsyncMock()
    bot.send_message.return_value.message_id = 123
    await deliver_announcements(SimpleNamespace(repo=repo, commerce=Commerce(factory, settings), _error=AsyncMock()), bot)
    markup = bot.send_message.call_args.kwargs['reply_markup']
    assert {'shop:home', 'settings:home'} <= {b.callback_data for row in markup.inline_keyboard for b in row}


async def test_settings_reset_needs_confirmation_and_replay_is_rejected():
    h = Harness()
    h.service.commerce = SimpleNamespace(snapshot=AsyncMock(return_value={'account': None}), set_promotions=AsyncMock())
    h.service.repo.reset_content = AsyncMock()
    await h.callback('settings:reset')
    message = next(c for c in reversed(h.session.calls) if isinstance(c, (SendMessage, EditMessageText)))
    token = message.reply_markup.inline_keyboard[0][0].callback_data
    h.reset_throttle()
    await h.callback('settings:confirm:made-up-token')
    h.service.repo.reset_content.assert_not_awaited()
    h.reset_throttle()
    await h.callback(token)
    h.service.repo.reset_content.assert_awaited_once_with(100)
    h.reset_throttle()
    await h.callback(token)
    assert h.service.repo.reset_content.await_count == 1
    await h.bot.session.close()


async def test_cancel_reset_invalidates_confirmation():
    h = Harness()
    h.service.commerce = SimpleNamespace(snapshot=AsyncMock(return_value={'account': None}))
    h.service.repo.reset_content = AsyncMock()
    await h.callback('settings:reset')
    message = next(c for c in reversed(h.session.calls) if isinstance(c, (SendMessage, EditMessageText)))
    token = message.reply_markup.inline_keyboard[0][0].callback_data
    h.reset_throttle()
    await h.callback('settings:home')
    h.reset_throttle()
    await h.callback(token)
    h.service.repo.reset_content.assert_not_awaited()
    await h.bot.session.close()


@pytest.mark.parametrize('action', ['admin:stats', 'admin:plans', 'admplan:1', 'admconfirm:fake'])
async def test_forged_admin_buttons_are_denied(action):
    h = Harness()
    h.service.commerce = SimpleNamespace(catalog=AsyncMock(), admin_self_plan=AsyncMock())
    await h.callback(action)
    h.service.commerce.catalog.assert_not_awaited()
    h.service.commerce.admin_self_plan.assert_not_awaited()
    assert 'только администратору' in h.text
    await h.bot.session.close()


async def test_plain_admin_word_opens_menu_and_stub_never_purchases():
    h = Harness()
    h.service.is_admin.return_value = True
    h.service.commerce = SimpleNamespace(admin_self_plan=AsyncMock())
    await h.message('admin')
    assert 'Админ-меню' in h.text
    await h.callback('purchase:stub')
    h.service.commerce.admin_self_plan.assert_not_awaited()
    await h.bot.session.close()


async def test_account_migration_preserves_entitlements_and_matches_models(store):
    repo, factory, settings = store
    await repo.admit_user(1)
    await Commerce(factory, settings).set_promotions(1, True)
    async with factory.begin() as session:
        (await session.get(Account, 1)).balance = 99
    migration = importlib.import_module('migrations.versions.0012_account_settings')
    def migrate(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True, 'compare_server_default': True})
        with Operations.context(context):
            migration.downgrade()
            migration.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(migrate)
    account = (await Commerce(factory, settings).snapshot(1))['account']
    assert account.balance == 99 and account.promotions_enabled


async def test_web_promotion_preview_filters_recipients_and_preserves_stub(admin):
    client, _, data, *_ = admin
    from app.models import User
    async with data.sessions.begin() as session:
        session.add_all([User(telegram_id=1), User(telegram_id=2)])
    async with data.sessions.begin() as session:
        session.add(Account(user_id=1, promotions_enabled=False))
    await login(client)
    page = await client.get('/announcements?template=discount')
    assert 'Предложения и скидки' in page.text and 'заглушка' in page.text
    form = BeautifulSoup(page.text, 'html.parser').select_one('#compose form')
    values = {t['name']: t.get('value', '') for t in form.select('input[name]')}
    response = await client.post('/announcements', data=values | TEMPLATES['discount'])
    assert response.status_code == 303
    detail = await client.get(response.headers['location'])
    assert 'Получателей: <b>1</b>' in detail.text and 'Посмотреть тарифы / купить' in detail.text
