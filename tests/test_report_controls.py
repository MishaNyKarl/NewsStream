import importlib
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from bs4 import BeautifulSoup
from sqlalchemy import func, select

from app.ai.client import AIClient
from app.bot import full_update_pages, notification_keyboard, notification_text
from app.commerce import Commerce
from app.domain import UserError
from app.models import Account, Base, CommerceAudit, Plan, User, utcnow
from app.report_controls import DEFAULTS, ReportControls, clip_words
from app.service import BotService
from app.worker import display_options
from test_admin import PASSWORD, admin as admin, csrf, login, password_hash as password_hash
from test_bot import Harness, change, story as ui_story
from test_commerce import plan
from test_repository import active, analysis, candidate, store as store
from test_service import setup_service, story


async def subscribed(store):
    repo, factory, settings = store
    item = await active(repo)
    lease = await repo.claim_story(item.id, user_id=1, manual=True)
    update = await repo.save_check(item.id, lease.lock_token, [candidate()], analysis())
    commerce = Commerce(factory, settings)
    await commerce.command('plan', plan(full_reports='1'), 'owner', str(uuid4()))
    async with factory.begin() as session:
        session.add(Account(user_id=1, plan_id=1, balance=75, expires_at=utcnow()+timedelta(days=1)))
    return repo, factory, settings, update, commerce


async def test_full_report_requires_current_feature_and_ownership_even_for_admin(store):
    repo, factory, settings, update, commerce = await subscribed(store)
    service = BotService(settings, repo, AsyncMock(), AsyncMock(), AsyncMock())
    assert (await service.get_full_update(1, update.id))[0].id == update.id
    await repo.admit_user(2)
    assert await service.get_full_update(2, update.id) is None
    await commerce.command('plan_features', dict(plan_id='1', full_reports='0', reason='Disable full report'), 'owner', str(uuid4()))
    with pytest.raises(UserError, match='подписке'):
        await service.get_full_update(1, update.id)
    await commerce.command('plan_features', dict(plan_id='1', full_reports='1', reason='Enable full report'), 'owner', str(uuid4()))
    async with factory.begin() as session:
        (await session.get(Account, 1)).expires_at = utcnow()-timedelta(seconds=1)
        (await session.get(User, 1)).is_admin = True
    with pytest.raises(UserError, match='подписке'):
        await service.get_full_update(1, update.id)
    assert not (await commerce.snapshot(2))['limits']['full_reports']


async def test_feature_edit_is_audited_idempotent_and_preserves_other_terms(store):
    _, factory, _, _, commerce = await subscribed(store)
    key = str(uuid4())
    values = dict(plan_id='1', full_reports='0', reason='Compare subscription value')
    await commerce.command('plan_features', values, 'owner', key)
    await commerce.command('plan_features', values, 'owner', key)
    async with factory() as session:
        tariff = await session.get(Plan, 1)
        assert not tariff.full_reports and tariff.price_minor == 99000 and tariff.period_days == 30
        assert (await session.get(Account, 1)).balance == 75
        assert await session.scalar(select(func.count()).select_from(CommerceAudit).where(CommerceAudit.key == key)) == 1
    with pytest.raises(UserError):
        await commerce.command('plan_features', values | {'full_reports':'invalid'}, 'owner', str(uuid4()))


async def test_old_full_button_shows_upgrade_without_reading_contents():
    h = Harness()
    h.service.full_reports_allowed.return_value = False
    await h.callback('full:12')
    assert 'функция подписки' in h.text
    h.service.repo.get_full_update.assert_not_awaited()
    assert 'shop:home' in str(h.session.calls)
    await h.bot.session.close()


def payload(**changes):
    return dict(DEFAULTS, version='0', reason='Experiment with report sizes') | changes


async def test_runtime_save_survives_new_instance_is_idempotent_and_rejects_stale(store):
    _, factory, _ = store
    controls = ReportControls(factory)
    key = str(uuid4())
    await controls.save(payload(preview_words='25', search_queries='1'), 'owner', key)
    await controls.save(payload(preview_words='25', search_queries='1'), 'owner', key)
    snapshot = await ReportControls(factory).snapshot()
    assert snapshot['values']['preview_words'] == 25 and snapshot['values']['search_queries'] == 1
    assert snapshot['version'] == 1
    with pytest.raises(UserError, match='изменились'):
        await controls.save(payload(), 'owner', str(uuid4()))
    await controls.save(payload(version='1'), 'owner', str(uuid4()))
    assert (await controls.snapshot())['values'] == DEFAULTS


async def test_worker_reads_actual_entitlements_and_persisted_display_options(store):
    repo, factory, settings, _, commerce = await subscribed(store)
    await repo.admit_user(2)
    await ReportControls(factory).save(payload(preview_words='23'), 'owner', str(uuid4()))
    service = BotService(settings, repo, AsyncMock(), AsyncMock(), AsyncMock())
    enabled, options = await display_options(service, 1)
    assert enabled and options['preview_words'] == 23
    assert not (await display_options(service, 2))[0]
    await commerce.command('plan_features', dict(plan_id='1', full_reports='0', reason='Turn feature off'), 'owner', str(uuid4()))
    assert not (await display_options(service, 1))[0]


@pytest.mark.parametrize('changes', [dict(preview_words='-1'), dict(full_words='bad'), dict(search_queries='7'),
    dict(source_reads='13'), dict(source_reads='1', analysis_sources='2'), dict(source_words='1501')])
async def test_runtime_invalid_limits_do_not_write(store, changes):
    _, factory, _ = store
    controls = ReportControls(factory)
    with pytest.raises(UserError):
        await controls.save(payload(**changes), 'owner', str(uuid4()))
    assert (await controls.snapshot())['version'] == 0


async def test_search_controls_bound_requests_without_changing_global_settings():
    service = setup_service()
    item = story()
    item.search_queries = ['query one', 'query two', 'query three']
    service.search.search.return_value = []
    await service._candidates(item, options=DEFAULTS | {'search_queries': 1, 'source_reads': 2, 'analysis_sources': 2})
    assert service.search.search.await_count == 1
    assert service.settings.max_search_queries_per_story == 3


async def test_model_uses_configured_source_and_word_budgets():
    service = setup_service()
    client = AIClient(service.settings)
    client._complete = AsyncMock(return_value=analysis(meaningful=False))
    source = candidate()
    source.content_excerpt = 'one two three four five'
    await client.analyze({'_report_controls':DEFAULTS | {'analysis_sources':1, 'source_words':2, 'preview_words':25}},
                         [source, candidate(2)])
    prompt, data, *_ = client._complete.call_args.args
    assert '25 слов' in prompt
    assert len(data['sources']) == 1 and data['sources'][0]['content'] == 'one two…'


def test_word_limits_preserve_saved_text_and_safe_full_pages():
    update = change(summary='word ' * 500, new_facts=['fact ' * 100], reason='reason ' * 100,
                    new_state='State', source_urls=['https://example.org/news'])
    short = notification_text(ui_story(), update, word_limit=20)
    long = notification_text(ui_story(), update, word_limit=120)
    assert len(short.split()) < len(long.split())
    full = ''.join(full_update_pages(ui_story(), update))
    limited = ''.join(full_update_pages(ui_story(), update, word_limit=30))
    assert 'Объём ограничен' in limited and 'https://example.org/news' in limited
    assert len(limited) < len(full) and update.summary == 'word ' * 500
    assert '🔒' in notification_keyboard(ui_story(), update).inline_keyboard[0][0].text
    assert '🔒' not in notification_keyboard(ui_story(), update, full_reports=True).inline_keyboard[0][0].text
    assert clip_words('😀 <tag> & word', 2) == '😀 <tag>…'


async def test_new_migrations_match_models_and_only_paid_existing_plans_gain_feature(store):
    repo, factory, settings = store
    await repo.admit_user(1)
    commerce = Commerce(factory, settings)
    await commerce.command('plan', plan(), 'owner', str(uuid4()))
    await commerce.command('plan', plan(price_minor='0'), 'owner', str(uuid4()))
    feature = importlib.import_module('migrations.versions.0013_full_report_entitlement')
    knobs = importlib.import_module('migrations.versions.0014_report_controls')
    def migrate(connection):
        context = MigrationContext.configure(connection, opts={'compare_type':True, 'compare_server_default':True})
        with Operations.context(context):
            knobs.downgrade()
            feature.downgrade()
            feature.upgrade()
            knobs.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(migrate)
    async with factory() as session:
        assert (await session.get(Plan, 1)).full_reports
        assert not (await session.get(Plan, 2)).full_reports


def fields(form):
    return {t['name']:t.get('value','') for t in form.select('input[name]')}


async def test_admin_preview_does_not_save_and_actual_save_requires_csrf(admin):
    client, _, data, *_ = admin
    await login(client)
    page = await client.get('/report-settings')
    assert page.status_code == 200 and 'без перезапуска' in page.text
    values = fields(BeautifulSoup(page.text, 'html.parser').select_one('#report-controls'))
    preview = await client.post('/report-settings', data=values | {'action':'preview', 'preview_words':'20'})
    assert preview.status_code == 200 and 'ещё не сохранены' in preview.text
    assert (await ReportControls(data.sessions).snapshot())['version'] == 0
    response = await client.post('/report-settings', data=values | {'action':'save', 'preview_words':'25'})
    assert response.status_code == 303
    assert (await ReportControls(data.sessions).snapshot())['values']['preview_words'] == 25
    response = await client.post('/report-settings', data=values | {'csrf':'invalid','action':'save'})
    assert response.status_code == 403


@pytest.mark.parametrize('role', ['viewer', 'finance'])
async def test_other_web_roles_cannot_change_report_controls_or_features(admin, password_hash, role):
    client, app, *_ = admin
    app.state.set_web_account('restricted-test', password_hash, role, True, 'owner')
    page = await client.get('/login')
    await client.post('/login', data={'csrf':csrf(page),'username':'restricted-test','password':PASSWORD})
    page = await client.get('/report-settings')
    assert page.status_code == 200
    assert (await client.post('/report-settings', data={'csrf':csrf(page),'action':'save'})).status_code == 403
    page = await client.get('/commerce')
    assert (await client.post('/commerce', data={'csrf':csrf(page),'action':'plan_features'})).status_code == 403
