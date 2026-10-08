import asyncio
import importlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.bot import build_router
from app.domain import UserError
from app.models import Base, Story, UserNews, utcnow
from app.repository import Repository
from app.worker import deliver_news_ready
from test_bot import Harness, news
from test_progress import service_with_results
from test_repository import active, extraction
from test_repository import store as store


async def ready(repo, user_id=1, message_id=None, text='Новость про запуск сервиса'):
    await repo.admit_user(user_id)
    item = await repo.save_user_news(user_id, text, input_message_id=message_id)
    claim = await repo.claim_user_news(user_id, item.id)
    return await repo.finish_user_news(user_id, item.id, claim.processing_token, extraction())


def real_harness(repo):
    service = service_with_results()
    service.repo = repo
    service.ai.extract.return_value = extraction()
    harness = Harness()
    harness.service = service
    harness.router = build_router(service, harness.settings)
    harness.dispatcher = Dispatcher()
    harness.dispatcher.include_router(harness.router)
    return harness, service


def buttons(call):
    return [b.callback_data for row in call.reply_markup.inline_keyboard for b in row]


async def test_raw_input_saved_before_analysis_failure_and_ready_reopens_without_ai(store):
    repo, _, _ = store
    await repo.admit_user(100)
    harness, service = real_harness(repo)
    service.ai.extract.side_effect = RuntimeError('private-model-secret')
    await harness.message('Новость про запуск сервиса')
    item = (await repo.list_user_news(100))[0]
    assert item.status == 'failed' and item.original_text == 'Новость про запуск сервиса'
    assert 'private-model-secret' not in item.error_message
    assert await repo.list_stories(100) == []
    service.ai.extract.side_effect = None
    harness.reset_throttle()
    await harness.callback(f'nretry:{item.id}')
    assert (await repo.get_user_news(100, item.id)).status == 'ready'
    assert service.ai.extract.await_count == 2
    service.provider_ready = lambda: False
    for action in ['nlater', 'nopen', 'nretry']:
        harness.reset_throttle()
        await harness.callback(f'{action}:{item.id}')
    assert service.ai.extract.await_count == 2
    assert (await repo.get_user_news(100, item.id)).notice_suppressed
    assert await repo.pending_news_notices() == []


async def test_inputs_that_fail_validation_are_retained_and_can_be_deleted(store):
    repo, _, _ = store
    await repo.admit_user(100)
    harness, service = real_harness(repo)
    await harness.message('abc')
    item = (await repo.list_user_news(100))[0]
    assert item.status == 'failed' and item.original_text == 'abc'
    service.ai.extract.assert_not_awaited()
    harness.reset_throttle()
    await harness.callback(f'ndelete:{item.id}')
    assert await repo.get_user_news(100, item.id)
    harness.reset_throttle()
    await harness.callback(f'ndelete_yes:{item.id}')
    assert await repo.get_user_news(100, item.id) is None


async def test_rapid_submissions_are_saved_even_when_analysis_is_throttled(store):
    repo, _, _ = store
    await repo.admit_user(100)
    harness, service = real_harness(repo)
    from app.bot import AccessMiddleware
    for middleware in harness.router.message.outer_middleware:
        if isinstance(middleware, AccessMiddleware):
            middleware.clock = lambda: 100.0
    await harness.message('Первая новость про запуск сервиса')
    await harness.message('Вторая новость про открытие станции')
    saved = await repo.list_user_news(100)
    assert len(saved) == 2
    assert saved[0].status == 'pending' and saved[0].original_text.startswith('Вторая')
    assert saved[1].status == 'ready'
    service.ai.extract.assert_awaited_once()


async def test_same_telegram_message_is_saved_once_under_concurrent_redelivery(store):
    repo, factory, settings = store
    await repo.admit_user(1)
    peers = [Repository(settings, factory) for _ in range(4)]
    items = await asyncio.gather(*(p.save_user_news(1, 'Исходный текст', input_message_id=99) for p in peers))
    assert len({item.id for item in items}) == 1
    assert len(await repo.list_user_news(1)) == 1
    await repo.admit_user(2)
    other = await repo.save_user_news(2, 'Чужой текст', input_message_id=99)
    assert other.id != items[0].id


async def test_owner_checks_cover_read_processing_choices_delete_and_notice_suppression(store):
    repo, _, _ = store
    item = await ready(repo)
    await repo.admit_user(2)
    assert await repo.get_user_news(2, item.id) is None
    assert await repo.list_user_news(2) == []
    for operation in [repo.claim_user_news, repo.user_news_story, repo.user_news_interest, repo.defer_user_news]:
        with pytest.raises(UserError):
            await operation(2, item.id)
    await repo.delete_user_news(2, item.id)
    assert await repo.get_user_news(1, item.id) is not None


async def test_pagination_is_stable_when_new_submissions_arrive(store):
    repo, _, _ = store
    await repo.admit_user(1)
    ids = [(await repo.save_user_news(1, f'Новость {i}')).id for i in range(12)]
    await ready(repo, 2)
    first = await repo.list_user_news(1)
    await repo.save_user_news(1, 'Новая новость')
    rest = await repo.list_user_news(1, first[7].id)
    assert [item.id for item in first[:8] + rest] == list(reversed(ids))


async def test_processing_claim_is_exclusive_and_stale_result_cannot_replace_retry(store):
    repo, factory, settings = store
    await repo.admit_user(1)
    item = await repo.save_user_news(1, 'Текст новости')
    claims = await asyncio.gather(*(Repository(settings, factory).claim_user_news(1, item.id)
                                   for _ in range(3)), return_exceptions=True)
    winners = [c for c in claims if not isinstance(c, Exception)]
    assert len(winners) == 1
    async with factory.begin() as db:
        row = await db.get(UserNews, item.id)
        row.processing_until = utcnow() - timedelta(seconds=1)
    new_claim = await repo.claim_user_news(1, item.id)
    assert new_claim.processing_token != winners[0].processing_token
    with pytest.raises(UserError):
        await repo.finish_user_news(1, item.id, winners[0].processing_token, extraction())
    await repo.finish_user_news(1, item.id, new_claim.processing_token, extraction())


async def test_cancelled_processing_retains_retryable_input(store):
    repo, _, _ = store
    await repo.admit_user(100)
    item = await repo.save_user_news(100, 'Текст новости для анализа')
    _, service = real_harness(repo)
    entered = asyncio.Event()
    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    service.ai.extract.side_effect = pending
    task = asyncio.create_task(service.process_user_news(100, item.id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    current = await repo.get_user_news(100, item.id)
    assert current.status == 'failed' and current.processing_token is None
    assert current.original_text == item.original_text


async def test_deleting_during_processing_never_resurrects_news(store):
    repo, _, _ = store
    await repo.admit_user(1)
    item = await repo.save_user_news(1, 'Текст новости')
    claim = await repo.claim_user_news(1, item.id)
    await repo.delete_user_news(1, item.id)
    with pytest.raises(UserError):
        await repo.finish_user_news(1, item.id, claim.processing_token, extraction())
    assert await repo.list_user_news(1) == []
    assert await repo.pending_news_notices() == []


async def test_following_from_saved_card_is_idempotent_and_redaction_preserves_news(store):
    repo, factory, settings = store
    item = await ready(repo)
    stories = await asyncio.gather(*(Repository(settings, factory).user_news_story(1, item.id) for _ in range(3)))
    assert len({s.id for s in stories}) == 1
    story = await repo.activate_story(1, stories[0].id)
    assert (await repo.get_user_news(1, item.id)).notice_suppressed
    await repo.set_status(1, story.id, 'deleted')
    saved = await repo.get_user_news(1, item.id)
    assert saved.original_text == item.original_text and saved.parsed_data == item.parsed_data
    restored = await repo.user_news_story(1, item.id)
    assert restored.id != story.id and restored.original_input == item.original_text
    await repo.activate_story(1, restored.id)
    await repo.delete_user_news(1, item.id)
    assert (await repo.get_story(1, restored.id)).status == 'active'


async def test_interests_do_not_consume_story_slots_and_duplicate_taps_do_not_duplicate_interest(store):
    repo, factory, settings = store
    for _ in range(settings.max_stories_per_user):
        await active(repo)
    item = await ready(repo)
    with pytest.raises(UserError, match='Лимит'):
        await repo.user_news_story(1, item.id)
    results = await asyncio.gather(*(Repository(settings, factory).user_news_interest(1, item.id) for _ in range(3)))
    assert sum(r.created for r in results) == 1
    saved = (await repo.list_interests(1))[0]
    assert len(await repo.list_stories(1)) == settings.max_stories_per_user
    assert (await repo.get_user_news(1, item.id)).notice_suppressed
    await repo.remove_interest(1, saved.id)
    assert (await repo.user_news_interest(1, item.id)).created


async def test_legacy_interest_and_library_interest_share_one_preference(store):
    repo, _, _ = store
    item = await ready(repo)
    linked = await repo.user_news_story(1, item.id)
    saved = await repo.save_interest(1, linked.id)
    result = await repo.user_news_interest(1, item.id)
    assert result.interest.id == saved.interest.id and not result.created
    restored = await repo.user_news_story(1, item.id)
    await repo.activate_story(1, restored.id)
    assert not (await repo.user_news_interest(1, item.id)).created
    assert len(await repo.list_interests(1)) == 1


async def test_library_interest_and_later_observation_share_one_preference(store):
    repo, _, _ = store
    item = await ready(repo)
    first = await repo.user_news_interest(1, item.id)
    linked = await repo.user_news_story(1, item.id)
    await repo.activate_story(1, linked.id)
    repeated = await repo.save_interest(1, linked.id)
    assert not repeated.created and repeated.interest.id == first.interest.id
    assert (await repo.get_story(1, linked.id)).status == 'active'


async def test_expired_draft_slot_is_reclaimed_without_losing_saved_news(store):
    repo, factory, settings = store
    first = await ready(repo)
    old = await repo.user_news_story(1, first.id)
    async with factory.begin() as db:
        row = await db.get(Story, old.id)
        row.created_at = utcnow() - timedelta(days=2)
    for _ in range(settings.max_stories_per_user - 1):
        await active(repo)
    second = await ready(repo, text='Ещё одна новость')
    assert await repo.user_news_story(1, second.id)
    assert (await repo.get_user_news(1, first.id)).parsed_data


async def test_ready_notice_claims_are_exclusive_and_survive_transport_retry(store):
    repo, factory, settings = store
    item = await ready(repo, message_id=900)
    claims = await asyncio.gather(*(Repository(settings, factory).pending_news_notices() for _ in range(3)))
    claimed = [r for rows in claims for r in rows]
    assert len(claimed) == 1
    token = claimed[0].notice_token
    await repo.mark_news_notice(1, item.id, 'stale', True)
    assert (await repo.get_user_news(1, item.id)).notice_sent_at is None
    await repo.mark_news_notice(1, item.id, token, False, retry_after=120)
    assert await repo.pending_news_notices() == []
    async with factory.begin() as db:
        row = await db.get(UserNews, item.id)
        row.notice_locked_until = utcnow() - timedelta(seconds=1)
    reclaimed = (await repo.pending_news_notices())[0]
    await repo.mark_news_notice(1, item.id, token, True)
    assert (await repo.get_user_news(1, item.id)).notice_sent_at is None
    await repo.mark_news_notice(1, item.id, reclaimed.notice_token, True)
    assert await repo.pending_news_notices() == []


async def test_real_worker_sends_one_distinct_ready_notice_in_reply_to_submission(store):
    repo, _, _ = store
    item = await ready(repo, message_id=123)
    service = SimpleNamespace(repo=repo, _error=AsyncMock())
    bot = AsyncMock()
    await deliver_news_ready(service, bot)
    await deliver_news_ready(service, bot)
    bot.send_message.assert_awaited_once()
    call = bot.send_message.call_args
    assert call.args[0] == 1 and 'Новость обработана' in call.args[1]
    assert call.kwargs['reply_parameters'].message_id == 123
    assert call.kwargs['reply_parameters'].allow_sending_without_reply
    assert call.kwargs['disable_notification'] is False
    assert f'nwatch:{item.id}' in str(call.kwargs['reply_markup'])
    assert 'menu:0' in str(call.kwargs['reply_markup'])
    assert (await repo.get_user_news(1, item.id)).notice_sent_at


@pytest.mark.parametrize('state', ['deleted', 'suppressed'])
async def test_worker_rechecks_news_after_claim_before_sending(state):
    item = news()
    service = SimpleNamespace(repo=AsyncMock(), _error=AsyncMock())
    service.repo.pending_news_notices.return_value = [item]
    service.repo.get_user_news.return_value = None if state == 'deleted' else news(notice_suppressed=True)
    bot = AsyncMock()
    await deliver_news_ready(service, bot)
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize('kind', ['timeout', 'flood', 'blocked'])
async def test_ready_notice_failures_are_bounded_and_do_not_record_delivery(kind):
    item = news()
    service = SimpleNamespace(repo=AsyncMock(), _error=AsyncMock())
    service.repo.pending_news_notices.return_value = [item]
    service.repo.get_user_news.return_value = item
    method = SendMessage(chat_id=100, text='ready')
    error = {'timeout': TimeoutError('private-secret'),
        'flood': TelegramRetryAfter(method, 'slow down', retry_after=60),
        'blocked': TelegramForbiddenError(method, 'blocked')}[kind]
    bot = AsyncMock()
    bot.send_message.side_effect = error
    await deliver_news_ready(service, bot)
    service.repo.mark_news_notice.assert_awaited_once_with(100, 21, 'ready-lease', False,
        retry_after=60 if kind == 'flood' else 0)
    if kind == 'blocked':
        service.repo.defer_user_news.assert_awaited_once_with(100, 21, reason='delivery_forbidden')
        service.repo.record_product_event.assert_awaited_once_with(
            'delivery_forbidden', 100, 'forbidden:news:21')


async def test_home_always_sends_a_new_root_message_so_progress_cannot_overwrite_it():
    harness = Harness()
    await harness.callback('menu:0')
    assert not any(isinstance(c, EditMessageText) for c in harness.session.calls)
    root = next(c for c in harness.session.calls if isinstance(c, SendMessage))
    assert [b.callback_data for b in root.reply_markup.inline_keyboard[0]] == ['add:news']
    assert [b.callback_data for b in root.reply_markup.inline_keyboard[1]] == ['news:0', 'jp:week']
    harness.service.process_user_news.assert_not_awaited()


@pytest.mark.parametrize('command', ['/menu', '/news', '/watching', '/help', '/check_now'])
async def test_root_commands_and_submenus_are_navigable(command):
    harness = Harness()
    await harness.message(command)
    screens = [c for c in harness.session.calls if isinstance(c, (SendMessage, EditMessageText))]
    assert screens
    for screen in screens:
        assert 'menu:0' in buttons(screen) or {'news:0', 'jp:week'} <= set(buttons(screen))
    harness.service.process_user_news.assert_not_awaited()


async def test_long_original_text_is_downloadable_and_escapes_short_html():
    harness = Harness()
    harness.service.get_user_news.return_value = news(original_text='<script>Исходный текст</script>')
    await harness.callback('ninput:21')
    assert '&lt;script&gt;' in harness.text and '<script>' not in harness.text
    harness.reset_throttle()
    harness.service.get_user_news.return_value = news(original_text='Очень длинный текст ' * 500)
    await harness.callback('ninput:21')
    attachment = next(c for c in harness.session.calls if isinstance(c, SendDocument))
    assert attachment.document.data.decode() == harness.service.get_user_news.return_value.original_text
    assert 'menu:0' in buttons(attachment)


async def test_migration_backfills_retained_news_without_touching_stories_or_notifying_old_items(store):
    repo, factory, _ = store
    if factory.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Production migration roundtrip requires PostgreSQL')
    item = await active(repo)
    older = await active(repo)
    deleted = await active(repo)
    await repo.set_status(1, deleted.id, 'deleted')
    async with factory.begin() as db:
        row = await db.get(Story, older.id)
        row.created_at = utcnow() - timedelta(days=2)
    module = importlib.import_module('migrations.versions.0006_user_news')
    def roundtrip(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(roundtrip)
    saved = await repo.list_user_news(1)
    assert len(saved) == 2 and saved[0].original_text == item.original_input
    assert [entry.story_id for entry in saved] == [item.id, older.id]
    assert saved[0].story_id == item.id and saved[0].status == 'ready'
    assert await repo.pending_news_notices() == []
    assert (await repo.get_story(1, item.id)).current_state == item.current_state
    assert (await repo.user_news_story(1, saved[0].id)).id == item.id
    async with factory() as db:
        assert (await db.scalar(select(Story).where(Story.id == deleted.id))).status == 'deleted'
