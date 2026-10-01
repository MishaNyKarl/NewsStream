import asyncio
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, MessageOriginChannel, MessageOriginHiddenUser, PhotoSize, Video

from app.bot import preview_keyboard, story_keyboard
from app.domain import UserError
from app.telegram_input import AlbumMiddleware, extract_story_input
from test_bot import Harness, NOW, story
from test_progress import service_with_results
from test_repository import extraction
from test_repository import store as store

POST = 'Компания объявила о переносе открытия станции. Новую дату обещают сообщить в октябре.'
PHOTO = [PhotoSize(file_id='test-photo', file_unique_id='photo', width=800, height=600)]


def origin(username='test_news_channel'):
    return MessageOriginChannel(date=NOW, chat=Chat(id=-10012345, type='channel', title='Новости', username=username), message_id=123)


@pytest.mark.parametrize('media', ['text', 'photo', 'video'])
async def test_public_forwarded_news_reaches_creation(media):
    harness = Harness()
    fields = {'forward_origin': origin()}
    if media == 'text':
        await harness.message(POST, **fields)
    else:
        fields['caption'] = POST
        if media == 'photo':
            fields['photo'] = PHOTO
        else:
            fields['video'] = Video(file_id='test-video', file_unique_id='video', width=800, height=600, duration=5)
        await harness.message(**fields)
    call = harness.service.save_user_news.call_args
    assert call.args == (100, POST)
    assert call.kwargs['use_text'] is True
    assert call.kwargs['source_url'] == 'https://t.me/test_news_channel/123'
    assert 'Что произошло' in harness.text


@pytest.mark.parametrize('forward_origin', [origin(None), MessageOriginHiddenUser(date=NOW, sender_user_name='Hidden')])
async def test_private_or_hidden_forward_does_not_invent_public_url(forward_origin):
    harness = Harness()
    await harness.message(POST, forward_origin=forward_origin)
    assert harness.service.save_user_news.call_args.kwargs['source_url'] is None
    assert harness.service.save_user_news.call_args.kwargs['use_text'] is True


async def test_caption_also_works_without_forward_header():
    harness = Harness()
    await harness.message(caption=POST, photo=PHOTO)
    assert harness.service.save_user_news.call_args.args == (100, POST)
    assert harness.service.save_user_news.call_args.kwargs['use_text'] is True


async def test_forwarded_commands_are_content_not_actions_or_invites():
    harness = Harness()
    forwarded = '/start invite_secret Текст новости про открытие станции'
    await harness.message(forwarded, forward_origin=origin())
    assert harness.service.authorize.call_args.kwargs['start_arg'] == ''
    assert harness.service.save_user_news.call_args.args[1] == forwarded


async def test_forwarded_media_without_text_gets_actionable_answer():
    harness = Harness()
    await harness.message(photo=PHOTO, forward_origin=origin())
    harness.service.save_user_news.assert_not_awaited()
    assert 'нет текста новости' in harness.text and 'пока не распознаю' in harness.text


async def test_forwarded_media_respects_closed_access():
    harness = Harness(authorized=False)
    await harness.message(caption=POST, photo=PHOTO, forward_origin=origin())
    harness.service.save_user_news.assert_not_awaited()
    assert 'закрытый тест' in harness.text


async def test_album_caption_on_second_item_creates_exactly_one_preview():
    harness = Harness()
    for middleware in harness.router.message.outer_middleware:
        if isinstance(middleware, AlbumMiddleware):
            middleware.wait_seconds = 0.04
    first = asyncio.create_task(harness.message(photo=PHOTO, forward_origin=origin(), media_group_id='album'))
    await asyncio.sleep(0.01)
    second = asyncio.create_task(harness.message(photo=PHOTO, caption=POST, forward_origin=origin(), media_group_id='album'))
    await asyncio.gather(first, second)
    harness.service.save_user_news.assert_awaited_once()
    assert harness.service.save_user_news.call_args.args == (100, POST)
    assert 'Слишком быстро' not in harness.text and 'нет текста новости' not in harness.text
    previews = [call for call in harness.session.calls if isinstance(call, EditMessageText) and 'Что произошло' in call.text]
    assert len(previews) == 1
    # Late remaining images from the same album cannot duplicate the observation.
    await harness.message(photo=PHOTO, forward_origin=origin(), media_group_id='album')
    assert harness.service.save_user_news.await_count == 1


async def test_captionless_album_produces_one_help_message():
    harness = Harness()
    for middleware in harness.router.message.outer_middleware:
        if isinstance(middleware, AlbumMiddleware):
            middleware.wait_seconds = 0.01
    await asyncio.gather(*(harness.message(photo=PHOTO, media_group_id='empty') for _ in range(3)))
    sends = [call for call in harness.session.calls if isinstance(call, SendMessage)]
    assert len(sends) == 1 and 'нет текста новости' in sends[0].text
    harness.service.save_user_news.assert_not_awaited()


def test_album_combines_distinct_captions_and_preserves_post_reference():
    first = Message(message_id=1, date=NOW, chat=Chat(id=100, type='private'), caption=POST, forward_origin=origin())
    second = first.model_copy(update={'message_id': 2, 'caption': 'Дополнение: подрядчика выберут на следующей неделе.'})
    third = first.model_copy(update={'message_id': 3})
    seed = extract_story_input(first, [third, second, first])
    assert seed.text == POST + '\n\n' + second.caption
    assert seed.source_url == 'https://t.me/test_news_channel/123'


def test_original_post_button_is_available_in_preview_and_story():
    item = story(original_url='https://t.me/test_news_channel/123')
    for keyboard in (preview_keyboard(item), story_keyboard(item)):
        assert any(button.url == item.original_url for row in keyboard.inline_keyboard for button in row)


async def test_forwarded_prose_uses_its_text_despite_footer_urls():
    service = service_with_results()
    service.ai.extract.return_value = extraction()
    post = POST + '\nПодписаться: https://t.me/example_channel'
    source = 'https://t.me/test_news_channel/123'
    result = await service.prepare_story(100, post, source_url=source, use_text=True)
    assert result is service.repo.create_draft.return_value
    service.fetcher.fetch.assert_not_awaited()
    service.ai.extract.assert_awaited_once_with(post, url=source)
    service.repo.create_draft.assert_awaited_once_with(100, post, source, service.ai.extract.return_value)


async def test_link_only_forward_explains_how_to_send_content():
    service = service_with_results()
    with pytest.raises(UserError, match='почти нет текста'):
        await service.prepare_story(100, 'https://t.me/channel/123', use_text=True)
    service.ai.extract.assert_not_awaited()
    service.fetcher.fetch.assert_not_awaited()


async def test_direct_url_keeps_existing_fetch_behavior():
    service = service_with_results()
    service.ai.extract.return_value = extraction()
    await service.prepare_story(100, 'https://example.org/article')
    service.fetcher.fetch.assert_awaited_once_with('https://example.org/article')


async def test_album_collection_does_not_bypass_authorization():
    harness = Harness(authorized=False)
    for middleware in harness.router.message.outer_middleware:
        if isinstance(middleware, AlbumMiddleware):
            middleware.wait_seconds = 0.01
    await asyncio.gather(harness.message(photo=PHOTO, media_group_id='x'),
                         harness.message(photo=PHOTO, caption=POST, media_group_id='x'))
    harness.service.save_user_news.assert_not_awaited()


async def test_album_cancellation_releases_buffer():
    middleware = AlbumMiddleware(wait_seconds=1)
    msg = Message(message_id=1, date=NOW, chat=Chat(id=100, type='private'),
                  from_user={'id':100, 'is_bot':False, 'first_name':'Tester'}, media_group_id='x', photo=PHOTO)
    task = asyncio.create_task(middleware(AsyncMock(), msg, {}))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert middleware.pending == {}


async def test_unusually_late_album_caption_is_not_discarded():
    middleware = AlbumMiddleware(wait_seconds=0.001)
    msg = Message(message_id=1, date=NOW, chat=Chat(id=100, type='private'),
                  from_user={'id':100, 'is_bot':False, 'first_name':'Tester'}, media_group_id='late', photo=PHOTO)
    handler = AsyncMock()
    await middleware(handler, msg, {})
    captioned = msg.model_copy(update={'message_id':2, 'caption':POST})
    await middleware(handler, captioned, {})
    assert handler.await_count == 2
    assert handler.call_args.args[0].caption == POST


async def test_forwarded_caption_through_router_service_and_real_database(store):
    from aiogram import Dispatcher
    from app.bot import build_router
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
    post = POST + '\nПодписаться: https://t.me/another_channel'
    await harness.message(photo=PHOTO, caption=post, forward_origin=origin())
    previews = [call for call in harness.session.calls if isinstance(call, EditMessageText) and call.reply_markup]
    assert len(previews) == 1
    watch = next(button.callback_data for row in previews[0].reply_markup.inline_keyboard
                 for button in row if (button.callback_data or '').startswith('nwatch:'))
    news_id = int(watch.split(':')[1])
    saved = await repo.get_user_news(100, news_id)
    assert saved.status == 'ready' and saved.original_text == post
    assert saved.source_url == 'https://t.me/test_news_channel/123'
    assert await repo.list_stories(100) == []
    service.fetcher.fetch.assert_not_awaited()
    await harness.callback(watch)
    saved = await repo.get_user_news(100, news_id)
    assert (await repo.get_story(100, saved.story_id)).status == 'active'
