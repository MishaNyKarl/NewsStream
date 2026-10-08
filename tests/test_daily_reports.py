import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.bot import _units, full_update_pages
from app.daily_reports import next_report, parse_time
from app.domain import UserError
from app.models import Base, DailyReport, StoryUpdate, utcnow
from app.repository import Repository
from app.worker import daily_report_page, deliver_daily_reports
from test_repository import active, analysis, candidate, store as store
from test_bot import Harness, change, story as ui_story


def test_time_uses_moscow_and_moves_exact_time_to_tomorrow():
    now = datetime(2026, 10, 8, 6, 30, tzinfo=timezone.utc)
    assert next_report(parse_time('09:30'), now).astimezone(timezone.utc) == now + timedelta(days=1)
    assert next_report(parse_time('09:31'), now).astimezone(timezone.utc) == now + timedelta(minutes=1)
    for invalid in ('24:00', '09:60', '9:30', '-1:00', '<b>09:30</b>'):
        with pytest.raises(UserError):
            parse_time(invalid)


async def make_due(repo, factory, owner=1):
    await repo.set_report_time(owner, '09:00')
    async with factory.begin() as session:
        pref = await session.get(DailyReport, owner)
        pref.next_at = utcnow() - timedelta(minutes=1)


async def test_daily_development_waits_for_digest_and_full_update_is_owned(store):
    repo, factory, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id)
    update = await repo.save_check(story.id, claim.lock_token, [candidate()], analysis())
    await repo.finish_check(story.id, claim.lock_token)
    assert await repo.pending_notifications() == []
    assert (await repo.get_full_update(1, update.id))[0].summary == update.summary
    assert await repo.get_full_update(2, update.id) is None
    await make_due(repo, factory)
    pref = await repo.claim_daily_report()
    assert pref.payload[0]['updates'][0]['id'] == update.id
    assert pref.payload[0]['checked']
    await repo.set_status(1, story.id, 'deleted')
    assert await repo.get_full_update(1, update.id) is None


async def test_same_time_queue_serializes_workers_and_rejects_stale_token(store):
    repo, factory, settings = store
    await active(repo, 1)
    await active(repo, 2)
    await make_due(repo, factory, 1)
    await make_due(repo, factory, 2)
    peer = Repository(settings, factory)
    claims = await asyncio.gather(repo.claim_daily_report(), peer.claim_daily_report())
    assert sum(item is not None for item in claims) == 1
    first = next(item for item in claims if item is not None)
    assert first.user_id == 1
    assert await peer.claim_daily_report() is None
    assert not await peer.advance_daily_report(1, 'stale-token')
    await repo.advance_daily_report(1, first.token)
    second = await peer.claim_daily_report()
    assert second.user_id == 2


async def test_retry_retains_snapshot_and_progress_across_restart(store):
    repo, factory, settings = store
    await active(repo)
    await make_due(repo, factory)
    first = await repo.claim_daily_report()
    await repo.advance_daily_report(1, first.token, sent_parts=1)
    await repo.advance_daily_report(1, first.token, retry_after=60)
    assert await repo.claim_daily_report() is None
    async with factory.begin() as session:
        (await session.get(DailyReport, 1)).retry_at = utcnow() - timedelta(seconds=1)
    second = await Repository(settings, factory).claim_daily_report()
    assert second.sent_parts == 1
    assert second.payload == first.payload
    assert second.token != first.token
    assert not await repo.advance_daily_report(1, first.token)


async def test_intensive_still_delivers_but_does_not_flush_old_daily_updates(store):
    repo, _, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id)
    await repo.save_check(story.id, claim.lock_token, [candidate()], analysis())
    await repo.set_monitoring_mode(1, story.id, 'intensive')
    assert await repo.pending_notifications() == []
    update = await repo.save_check(story.id, claim.lock_token, [candidate(2)], analysis())
    assert (await repo.pending_notifications())[0][0].id == update.id


async def test_prompt_only_once_and_excludes_intensive(store):
    repo, _, _ = store
    story = await active(repo)
    await repo.set_monitoring_mode(1, story.id, 'intensive')
    assert await repo.claim_report_prompt() is None
    await repo.set_monitoring_mode(1, story.id, 'daily')
    assert await repo.claim_report_prompt(1) == 1
    assert await repo.claim_report_prompt() is None
    await repo.set_report_time(1, '00:00')
    assert (await repo.report_preference(1)).minute == 0
    assert await repo.claim_report_prompt() is None


def test_full_report_preserves_long_text_all_facts_and_safe_html():
    summary = '<script> & 😀 ' * 1000
    facts = ['fact-' + str(i) for i in range(15)]
    update = SimpleNamespace(summary=summary, new_facts=facts, reason='reason', new_state='state',
                             source_urls=['https://example.org/a', 'javascript:alert(1)'])
    pages = full_update_pages(SimpleNamespace(title='Title'), update)
    assert len(pages) > 1
    assert all(_units(page) <= 3500 for page in pages)
    text = unescape(''.join(pages))
    assert summary in text
    assert all(fact in text for fact in facts)
    assert 'javascript:' not in text
    assert all('<script>' not in page for page in pages)


def test_digest_distinguishes_no_changes_from_missing_check_and_limits_message():
    entries = [dict(story_id=i, title='😀<&' * 200, checked=checked, updates=updates)
               for i, checked, updates in [(1, True, []), (2, False, []),
                   (3, True, [dict(id=n, summary='<&😀' * 600, kind='development') for n in range(5)])]]
    text, keyboard = daily_report_page(entries, utcnow(), 1, 1)
    assert '— Без изменений' in text
    assert 'Нет свежей проверки' in text
    assert _units(text) < 3900
    assert any(b.callback_data == 'full:4' for row in keyboard.inline_keyboard for b in row)


async def test_worker_sends_digest_acknowledges_updates_and_skips_paused(store):
    repo, factory, _ = store
    story = await active(repo)
    claim = await repo.claim_story(story.id)
    update = await repo.save_check(story.id, claim.lock_token, [candidate()], analysis())
    await make_due(repo, factory)
    bot = AsyncMock()
    bot.send_message.return_value = SimpleNamespace(message_id=123)
    service = SimpleNamespace(repo=repo, _error=AsyncMock())
    await deliver_daily_reports(service, bot)
    assert bot.send_message.await_count == 1

    async with factory() as session:
        saved = await session.scalar(select(StoryUpdate).where(StoryUpdate.id == update.id))
        assert saved.notified_at and saved.telegram_message_id == 123
    assert (await repo.report_preference(1)).payload is None
    await make_due(repo, factory)
    await repo.claim_daily_report()
    async with factory.begin() as session:
        pref = await session.get(DailyReport, 1)
        pref.locked_until = utcnow() - timedelta(seconds=1)
    await repo.set_status(1, story.id, 'paused')
    await deliver_daily_reports(service, bot)
    assert bot.send_message.await_count == 1


async def test_first_subscription_prompts_and_time_choice_uses_clicking_owner():
    harness = Harness()
    harness.service.repo.claim_report_prompt.return_value = 100
    await harness.callback('watch:11')
    assert 'Во сколько' in harness.text
    harness.service.repo.claim_report_prompt.assert_awaited_once_with(100)
    harness.reset_throttle()
    await harness.callback('report:18:00')
    harness.service.repo.set_report_time.assert_awaited_once_with(100, '18:00')
    assert '18:00 МСК' in harness.text


async def test_custom_time_command_and_full_update_button():
    harness = Harness()
    await harness.message('/report 09:30')
    harness.service.repo.set_report_time.assert_awaited_once_with(100, '09:30')
    harness.service.repo.get_full_update.return_value = (change(summary='complete ' * 2000), ui_story())
    await harness.callback('full:12')
    harness.service.repo.get_full_update.assert_awaited_once_with(100, 12)
    assert harness.text.count('complete') == 2000
    harness.service.repo.get_full_update.return_value = None
    harness.reset_throttle()
    await harness.callback('full:13')
    assert 'Обновление удалено или недоступно' in harness.text


async def test_daily_report_migration_matches_metadata(store):
    _, factory, _ = store
    module = importlib.import_module('migrations.versions.0010_daily_reports')
    def migrate(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(migrate)


async def test_worker_retry_resumes_only_unsent_parts(store):
    repo, factory, settings = store
    settings.max_stories_per_user = 5
    for _ in range(4):
        await active(repo)
    await make_due(repo, factory)
    bot = AsyncMock()
    bot.send_message.side_effect = [SimpleNamespace(message_id=100), TimeoutError()]
    service = SimpleNamespace(repo=repo, _error=AsyncMock())
    await deliver_daily_reports(service, bot)
    assert (await repo.report_preference(1)).sent_parts == 1
    service._error.assert_awaited_once()
    async with factory.begin() as session:
        (await session.get(DailyReport, 1)).retry_at = utcnow() - timedelta(seconds=1)
    bot.send_message.reset_mock(side_effect=True)
    bot.send_message.return_value = SimpleNamespace(message_id=101)
    service.repo = Repository(settings, factory)
    await deliver_daily_reports(service, bot)
    assert bot.send_message.await_count == 1
    assert '2/2' in bot.send_message.call_args.args[1]
    assert (await repo.report_preference(1)).payload is None
