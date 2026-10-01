import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Chat, ForceReply, Message, User
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select

from app.domain import UserError
from app.bot import build_router
from app.journal import DATE_PROMPT, JournalWindow, date_window, parse_journal_callback, preset_window
from app.models import Base, StoryUpdate, utcnow
from test_bot import Harness, NOW, change, story
from test_repository import active, store as store
from test_progress import service_with_results


def record(story_id, notified_at=None, **kwargs):
    values = dict(story_id=story_id, summary='Новость', new_facts=['Факт'], previous_state='До', new_state='После',
                  importance_score=.9, confidence_score=.9, source_urls=['https://example.org'],
                  reason='Развитие', notified_at=notified_at, created_at=NOW)
    values.update(kwargs)
    return StoryUpdate(**values)


def test_periods_use_moscow_midnight_and_inclusive_calendar_end():
    now = datetime(2026, 10, 1, 0, 20, tzinfo=timezone.utc)
    today = preset_window('today', now)
    assert today.since == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)
    custom = date_window('29.09.2026 30.09.2026', now)
    assert custom.since == datetime(2026, 9, 28, 21, tzinfo=timezone.utc)
    assert custom.until == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)
    assert custom.label == '29.09.2026 — 30.09.2026 · МСК'
    assert date_window('30.09.2026', now).label == '30.09.2026 · МСК'
    assert preset_window('day', now).end - preset_window('day', now).start == 86400
    assert preset_window('all', now).start == 0


@pytest.mark.parametrize('value', ['31.09.2026', '30.09.2026 01.09.2026', '01.01.2099',
                                 'yesterday', '01.01.2026 02.01.2026 03.01.2026', '31.12.1969'])
def test_invalid_period_does_not_become_a_news_request(value):
    with pytest.raises(UserError):
        date_window(value, NOW)


@pytest.mark.parametrize('value', ['jn:2:1:0', 'ju:0:1:2:0', 'jn:1:2:9999999999',
                                 'jo:1:2:0', 'ju:1:1:2:0:0', 'jn:-1:2:0'])
def test_malformed_navigation_is_rejected(value):
    assert parse_journal_callback(value) is None


async def test_journal_filters_owner_delivery_period_and_keeps_paused_and_demo(store):
    repo, factory, _ = store
    first, second = await active(repo), await active(repo, 2)
    await repo.set_status(1, first.id, 'paused')
    window = JournalWindow(int(NOW.timestamp()), int((NOW + timedelta(days=1)).timestamp()))
    async with factory.begin() as db:
        records = [record(first.id, NOW), record(first.id, NOW + timedelta(seconds=1), is_demo=True),
                   record(first.id, window.until), record(first.id, NOW - timedelta(seconds=1)),
                   record(first.id), record(second.id, NOW)]
        db.add_all(records)
    rows = await repo.list_notifications(1, window)
    assert [item.id for item, _ in rows] == [records[1].id, records[0].id]
    assert await repo.get_notification(2, records[0].id, window) is None
    assert await repo.get_notification(1, records[4].id, window) is None
    assert await repo.get_notification(1, records[2].id, window) is None
    await repo.set_status(1, first.id, 'deleted')
    assert await repo.list_notifications(1, window) == []
    assert await repo.get_notification(1, records[0].id, window) is None


async def test_pagination_is_stable_with_tied_delivery_times_and_new_arrivals(store):
    repo, factory, _ = store
    item, other = await active(repo), await active(repo, 2)
    window = preset_window('all', NOW + timedelta(days=1))
    async with factory.begin() as db:
        records = [record(item.id, NOW) for _ in range(12)]
        foreign = record(other.id, NOW)
        db.add_all(records + [foreign])
    first = await repo.list_notifications(1, window)
    assert len(first) == 9
    async with factory.begin() as db:
        db.add(record(item.id, NOW + timedelta(seconds=1)))
    second = await repo.list_notifications(1, window, before_id=first[7][0].id)
    ids = [r[0].id for r in first[:8] + second]
    assert len(ids) == len(set(ids)) == 12
    assert ids == sorted([r.id for r in records], reverse=True)
    with pytest.raises(UserError):
        await repo.list_notifications(1, window, before_id=foreign.id)


async def test_reply_anchor_is_saved_only_with_successful_owned_delivery_lease(store):
    repo, factory, _ = store
    item = await active(repo)
    update = await repo.create_demo(1, item.id)
    claimed, _ = (await repo.pending_notifications())[0]
    await repo.mark_notified(update.id, True, delivery_token='stale', telegram_message_id=500)
    window = preset_window('all', utcnow() + timedelta(seconds=1))
    assert await repo.get_notification(1, update.id, window) is None
    await repo.mark_notified(update.id, True, delivery_token=claimed.delivery_lock_token, telegram_message_id=600)
    await repo.mark_notified(update.id, True, delivery_token=claimed.delivery_lock_token, telegram_message_id=700)
    saved, _ = await repo.get_notification(1, update.id, window)
    assert saved.telegram_message_id == 600 and saved.notified_at is not None


def journal_harness(items=None):
    harness = Harness()
    current = change(notified_at=NOW, update_kind='context', telegram_message_id=55)
    harness.service.list_notifications = AsyncMock(return_value=items if items is not None else [(current, story())])
    harness.service.get_notification = AsyncMock(return_value=(current, story()))
    return harness


async def test_real_router_lists_periods_without_ai_and_opens_detail_then_original_reply():
    harness = journal_harness()
    await harness.message('/journal')
    listing = next(c for c in harness.session.calls if isinstance(c, SendMessage))
    data = listing.reply_markup.inline_keyboard[0][0].callback_data
    parsed = parse_journal_callback(data)
    assert parsed[0] == 'ju' and parsed[3] == 12
    assert 'jp:custom' in str(listing.reply_markup) and 'jp:all' in str(listing.reply_markup)
    harness.reset_throttle()
    await harness.callback(data)
    detail = next(c for c in harness.session.calls if isinstance(c, EditMessageText))
    assert 'Отправлено:' in detail.text and 'Уточнение' in detail.text
    buttons = [b for row in detail.reply_markup.inline_keyboard for b in row]
    anchor = next(b for b in buttons if b.callback_data and b.callback_data.startswith('jo:'))
    back = next(b for b in buttons if b.text == '← Назад в журнал')
    assert parse_journal_callback(back.callback_data)[1] == parsed[1]
    assert max(len((b.callback_data or '').encode()) for b in buttons) <= 64
    harness.reset_throttle()
    await harness.callback(anchor.callback_data)
    reply = [c for c in harness.session.calls if isinstance(c, SendMessage)][-1]
    assert reply.reply_parameters.message_id == 55 and reply.reply_parameters.allow_sending_without_reply
    harness.service.prepare_story.assert_not_awaited()


async def test_old_notification_opens_without_inventing_original_link():
    harness = journal_harness()
    harness.service.get_notification.return_value[0].telegram_message_id = None
    await harness.callback(preset_window().callback('ju', update_id=12))
    detail = next(c for c in harness.session.calls if isinstance(c, EditMessageText))
    assert '↩ Показать в чате' not in str(detail.reply_markup)
    assert 'Объявлена дата открытия' in detail.text


async def test_unavailable_notification_does_not_expose_a_card():
    harness = journal_harness()
    harness.service.get_notification.return_value = None
    await harness.callback(preset_window().callback('ju', update_id=12))
    assert not any(isinstance(c, (SendMessage, EditMessageText)) for c in harness.session.calls)
    assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in harness.session.calls)


async def test_custom_dates_force_reply_and_validation_never_create_a_story():
    harness = journal_harness([])
    await harness.callback('jp:custom')
    prompt = next(c for c in harness.session.calls if isinstance(c, SendMessage))
    assert isinstance(prompt.reply_markup, ForceReply)
    original = Message(message_id=99, date=NOW, chat=Chat(id=100, type='private'), text=prompt.text,
                       from_user=User(id=777, is_bot=True, first_name='Bot'))
    harness.reset_throttle()
    await harness.message('31.02.2026', reply_to_message=original)
    assert 'Не удалось разобрать' in harness.text
    last = [c for c in harness.session.calls if isinstance(c, SendMessage) and isinstance(c.reply_markup, ForceReply)][-1]
    assert last.text.startswith(DATE_PROMPT) and isinstance(last.reply_markup, ForceReply)
    harness.reset_throttle()
    await harness.message('01.01.2026 02.01.2026', reply_to_message=original)
    assert 'уведомлений нет' in harness.text
    harness.service.prepare_story.assert_not_awaited()
    assert harness.service.list_notifications.await_count == 1


async def test_new_journal_migration_preserves_historical_delivery_and_matches_schema(store):
    repo, factory, _ = store
    if factory.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Production migration roundtrip requires PostgreSQL')
    item = await active(repo)
    async with factory.begin() as db:
        update = record(item.id, NOW)
        db.add(update)
    module = importlib.import_module('migrations.versions.0005_notification_journal')
    def upgrade(connection):
        context = MigrationContext.configure(connection, opts={'compare_type': True})
        with Operations.context(context):
            module.downgrade()
            module.upgrade()
        assert compare_metadata(context, Base.metadata) == []
    async with factory.kw['bind'].begin() as connection:
        await connection.run_sync(upgrade)
    async with factory() as db:
        current = await db.scalar(select(StoryUpdate).where(StoryUpdate.id == update.id))
        assert current.notified_at == NOW and current.telegram_message_id is None
        assert current.summary == 'Новость'


async def test_journal_router_service_database_roundtrip_works_without_llm(store):
    repo, _, _ = store
    item = await active(repo, 100)
    update = await repo.create_demo(100, item.id)
    pending, _ = (await repo.pending_notifications())[0]
    await repo.mark_notified(update.id, True, delivery_token=pending.delivery_lock_token, telegram_message_id=1234)
    service = service_with_results()
    service.repo = repo
    service.provider_ready = lambda: False
    harness = Harness()
    harness.router = build_router(service, harness.settings)
    harness.dispatcher = Dispatcher()
    harness.dispatcher.include_router(harness.router)
    await harness.message('/journal')
    listing = next(c for c in harness.session.calls if isinstance(c, SendMessage))
    button = listing.reply_markup.inline_keyboard[0][0]
    assert parse_journal_callback(button.callback_data)[3] == update.id
    harness.reset_throttle()
    await harness.callback(button.callback_data)
    detail = next(c for c in harness.session.calls if isinstance(c, EditMessageText))
    assert 'Демонстрационное обновление' in detail.text
    assert 'jo:' in str(detail.reply_markup)
    service.ai.extract.assert_not_awaited()
    service.ai.analyze.assert_not_awaited()
    service.search.search.assert_not_awaited()
