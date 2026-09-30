import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import func, select

from app.bot import (MAX_MESSAGE_UNITS, _units, build_router, interest_text,
                     notification_keyboard, preview_keyboard, story_keyboard)
from app.domain import UserError
from app.models import Story, UsageEvent, UserInterest
from app.repository import Repository
from test_bot import Harness, NOW, Tags, change, story
from test_progress import service_with_results
from test_repository import active, extraction, store as store
from test_telegram_input import PHOTO, POST, origin


async def draft(repo, user_id=1, text='Мне интересен запуск сервиса'):
    await repo.admit_user(user_id)
    return await repo.create_draft(user_id, text, 'https://example.org/news', extraction())


async def test_explicit_interest_persists_snapshot_without_monitoring_or_draft_quota(store):
    repo, factory, settings = store
    item = await draft(repo)
    result = await repo.save_interest(1, item.id)
    saved = result.interest
    assert result.created and result.monitoring_status == 'deleted'
    assert saved.user_id == 1 and saved.source_story_id == item.id
    assert saved.title == item.title and saved.summary == item.summary
    assert saved.entities == item.entities and saved.keywords == item.keywords
    assert saved.source_url == item.original_url and len(saved.input_fingerprint) == 64
    assert saved.created_at.tzinfo is not None
    assert await repo.list_stories(1) == [] and await repo.due_story_ids() == []
    async with factory() as session:
        original = await session.get(Story, item.id)
        assert original.status == 'deleted' and original.original_input == '' and original.next_check_at is None
    # Saving several standalone interests never fills the story/draft quota.
    for _ in range(settings.max_stories_per_user + 1):
        another = await draft(repo)
        await repo.save_interest(1, another.id)
    assert (await Repository(settings, factory).get_interest(1, saved.id)).keywords == item.keywords


@pytest.mark.parametrize('mode', ['active', 'paused', 'intensive'])
async def test_saving_and_removing_interest_do_not_change_existing_monitoring(store, mode):
    repo, _, _ = store
    item = await active(repo)
    if mode == 'paused':
        item = await repo.set_status(1, item.id, 'paused')
    elif mode == 'intensive':
        item = await repo.set_monitoring_mode(1, item.id, 'intensive')
    before = (item.status, item.next_check_at, item.monitoring_mode, item.intensive_started_at, item.intensive_until)
    saved = await repo.save_interest(1, item.id)
    assert saved.monitoring_status == item.status
    assert await repo.remove_interest(1, saved.interest.id)
    current = await repo.get_story(1, item.id)
    assert (current.status, current.next_check_at, current.monitoring_mode,
            current.intensive_started_at, current.intensive_until) == before


async def test_duplicate_clicks_from_multiple_processes_create_one_interest_and_event(store):
    repo, factory, settings = store
    item = await draft(repo)
    peers = [Repository(settings, factory) for _ in range(8)]
    results = await asyncio.gather(*(peer.save_interest(1, item.id) for peer in peers))
    assert sum(result.created for result in results) == 1
    assert len({result.interest.id for result in results}) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(UserInterest)) == 1
        assert await session.scalar(select(func.count()).select_from(UsageEvent).where(
            UsageEvent.operation == 'interest_saved')) == 1


async def test_interest_ownership_is_enforced_on_every_operation(store):
    repo, _, _ = store
    item = await draft(repo)
    await repo.admit_user(2)
    with pytest.raises(UserError):
        await repo.save_interest(2, item.id)
    with pytest.raises(UserError):
        await repo.save_interest(999, item.id)
    saved = (await repo.save_interest(1, item.id)).interest
    assert await repo.get_interest(2, saved.id) is None
    assert await repo.list_interests(2) == []
    assert not await repo.remove_interest(2, saved.id)
    assert await repo.get_interest(1, saved.id) is not None


async def test_story_deletion_preserves_explicit_interest_until_user_removes_it(store):
    repo, factory, _ = store
    item = await active(repo)
    saved = (await repo.save_interest(1, item.id)).interest
    await repo.set_status(1, item.id, 'deleted')
    kept = await repo.get_interest(1, saved.id)
    assert kept.title == item.title and kept.keywords == item.keywords
    assert await repo.remove_interest(1, saved.id)
    assert not await repo.remove_interest(1, saved.id)
    async with factory() as session:
        assert await session.get(UserInterest, saved.id) is None
    assert await repo.list_interests(1) == []


async def test_stale_remove_button_cannot_remove_readded_interest(store):
    repo, _, _ = store
    item = await active(repo)
    first = (await repo.save_interest(1, item.id)).interest
    await repo.remove_interest(1, first.id)
    second = (await repo.save_interest(1, item.id)).interest
    assert second.id != first.id
    assert not await repo.remove_interest(1, first.id)
    assert await repo.get_interest(1, second.id) is not None


async def test_cancelled_draft_cannot_be_used_as_new_interest(store):
    repo, _, _ = store
    item = await draft(repo)
    await repo.set_status(1, item.id, 'deleted')
    with pytest.raises(UserError):
        await repo.save_interest(1, item.id)
    assert await repo.list_interests(1) == []


async def test_interest_and_draft_retirement_are_one_transaction(store, monkeypatch):
    repo, factory, _ = store
    item = await draft(repo)
    monkeypatch.setattr(repo, '_redact', AsyncMock(side_effect=RuntimeError('storage failed')))
    with pytest.raises(RuntimeError):
        await repo.save_interest(1, item.id)
    assert await repo.list_interests(1) == []
    async with factory() as session:
        original = await session.get(Story, item.id)
        assert original.status == 'draft' and original.original_input == item.original_input


async def test_concurrent_watch_and_interest_never_revive_redacted_draft(store):
    repo, factory, settings = store
    item = await draft(repo)
    peer = Repository(settings, factory)
    results = await asyncio.gather(repo.save_interest(1, item.id), peer.activate_story(1, item.id),
                                   return_exceptions=True)
    assert not isinstance(results[0], Exception)
    async with factory() as session:
        current = await session.get(Story, item.id)
        if current.status == 'deleted':
            assert isinstance(results[1], UserError) and current.next_check_at is None
        else:
            assert current.status == 'active' and current.original_input and current.next_check_at


async def test_interests_require_explicit_action_and_list_paginates_without_leaks(store):
    repo, _, _ = store
    item = await active(repo)
    assert await repo.list_interests(1) == []
    for number in range(11):
        saved = await draft(repo, text=f'Тема {number}')
        await repo.save_interest(1, saved.id)
    foreign = await draft(repo, user_id=2)
    await repo.save_interest(2, foreign.id)
    first = await repo.list_interests(1)
    second = await repo.list_interests(1, before_id=first[7].id)
    assert len(first) == 9 and len(second) == 3
    all_items = first[:8] + second
    assert len({row.id for row in all_items}) == 11
    assert all(row.user_id == 1 for row in all_items)
    assert (await repo.get_story(1, item.id)).status == 'active'


@pytest.mark.parametrize('forwarded', [False, True])
async def test_input_through_real_router_service_database_to_interest_list_and_remove(store, forwarded):
    repo, _, _ = store
    await repo.admit_user(100)
    service = service_with_results()
    service.repo = repo
    service.ai.extract.return_value = extraction()
    harness = Harness()
    harness.service = service
    harness.router = build_router(service, harness.settings)
    harness.dispatcher = Dispatcher()
    harness.dispatcher.include_router(harness.router)
    if forwarded:
        await harness.message(photo=PHOTO, caption=POST, forward_origin=origin())
    else:
        await harness.message('Мне интересны новости про запуск сервиса')
    preview = next(call for call in harness.session.calls if isinstance(call, EditMessageText))
    action = next(button.callback_data for row in preview.reply_markup.inline_keyboard
                  for button in row if (button.callback_data or '').startswith('interest:'))
    # Preference buttons must work even if the model becomes unavailable.
    service.provider_ready = lambda: False
    await harness.callback(action)
    assert service.ai.extract.await_count == 1
    service.ai.analyze.assert_not_awaited()
    service.search.search.assert_not_awaited()
    saved = (await repo.list_interests(100))[0]
    assert saved.source_url == ('https://t.me/test_news_channel/123' if forwarded else None)
    assert 'Наблюдение не включено' in harness.text and 'Подборки пока не запущены' in harness.text
    assert await repo.list_stories(100) == []
    harness.reset_throttle()
    await harness.message('/interests')
    assert f'interest_view:{saved.id}' in str(harness.session.calls[-1].reply_markup)
    harness.reset_throttle()
    await harness.callback(f'interest_view:{saved.id}')
    assert saved.title in harness.session.calls[-1].text
    harness.reset_throttle()
    await harness.callback(f'interest_remove:{saved.id}')
    assert await repo.list_interests(100) == []


def test_interest_buttons_exist_on_preview_story_and_update_and_use_story_identity():
    item = story()
    for keyboard in (preview_keyboard(item), story_keyboard(item), notification_keyboard(item, change())):
        assert any(button.callback_data == 'interest:11' for row in keyboard.inline_keyboard for button in row)


def test_interest_card_escapes_user_content_and_fits_message_limit():
    hostile = '😀<&"' * 5000
    item = SimpleNamespace(title=hostile, summary=hostile, keywords=[hostile] * 12, created_at=NOW)
    rendered = interest_text(item)
    assert _units(rendered) < MAX_MESSAGE_UNITS
    parser = Tags()
    parser.feed(rendered)
    assert set(parser.tags) <= {'b'}


async def test_empty_interests_is_helpful_and_forwarded_command_is_still_news():
    harness = Harness()
    harness.service.list_interests = AsyncMock(return_value=[])
    await harness.message('/interests')
    assert 'Пока пусто' in harness.text and 'Просто интересна тема' in harness.text
    harness.reset_throttle()
    await harness.message('/interests — новости науки и космоса', forward_origin=origin())
    harness.service.prepare_story.assert_awaited_once()
    assert harness.service.list_interests.await_count == 1


async def test_unavailable_interest_does_not_display_private_data():
    harness = Harness()
    harness.service.get_interest = AsyncMock(return_value=None)
    await harness.callback('interest_view:99')
    harness.service.get_interest.assert_awaited_once_with(100, 99)
    assert any('недоступен' in call.text for call in harness.session.calls if isinstance(call, SendMessage))
