from datetime import datetime, timezone
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot import (
    AccessMiddleware, MAX_MESSAGE_UNITS, _units, build_router, notification_keyboard,
    notification_text, parse_callback, preview_text, safe_url, story_text,
)
from app.domain import UserError


NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)


def story(**kwargs):
    values = dict(id=11, title="Открытие станции", summary="Строительство продолжается.",
                  current_state="Ожидается новая дата открытия.", watch_goals=["Срок открытия", "Изменения проекта"],
                  status="active", check_frequency_hours=24, last_checked_at=None,
                  last_meaningful_update_at=None)
    values.update(kwargs)
    return SimpleNamespace(**values)


def change(**kwargs):
    values = dict(id=12, summary="Объявлена дата открытия.", new_facts=["Открытие в декабре"],
                  reason="Появился конкретный срок.", source_urls=["https://example.org/news?a=1&b=2"],
                  created_at=NOW, is_demo=False)
    values.update(kwargs)
    return SimpleNamespace(**values)

def news(**kwargs):
    values = dict(id=21, user_id=100, original_text='Следить за открытием станции', source_url=None,
        use_text=True, input_message_id=1, status='ready', story_id=None, created_at=NOW, updated_at=NOW,
        processing_until=None, error_message=None, notice_suppressed=False, notice_sent_at=None,
        notice_token='ready-lease', parsed_data=dict(title='Открытие станции',
            short_summary='Строительство продолжается.', current_state='Ожидается новая дата открытия.',
            entities=['Станция'], keywords=['строительство'], search_queries=['строительство станции'],
            watch_goals=['Срок открытия', 'Изменения проекта']))
    values.update(kwargs)
    return SimpleNamespace(**values)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=777, is_bot=True, first_name="Test", username="news_test_bot")
        if isinstance(method, AnswerCallbackQuery):
            return True
        return Message(message_id=99, date=NOW, chat=Chat(id=100, type="private"), text=getattr(method, "text", "")).as_(bot)

    async def stream_content(self, *args, **kwargs):
        yield b""


class Harness:
    def __init__(self, authorized=True):
        self.session = FakeSession()
        self.bot = Bot("777:FAKE_TOKEN_FOR_TESTS_ONLY", session=self.session)
        self.service = SimpleNamespace(
            authorize=AsyncMock(return_value=SimpleNamespace(telegram_id=100) if authorized else None),
            is_admin=AsyncMock(return_value=False), provider_ready=lambda: True,
            prepare_story=AsyncMock(return_value=story(status="draft")),
            save_user_news=AsyncMock(return_value=news(status='pending')),
            process_user_news=AsyncMock(return_value=news()),
            list_user_news=AsyncMock(return_value=[news()]), get_user_news=AsyncMock(return_value=news()),
            user_news_story=AsyncMock(return_value=story(status='draft')),
            user_news_interest=AsyncMock(), defer_user_news=AsyncMock(), delete_user_news=AsyncMock(),
            confirm_story=AsyncMock(return_value=story()), cancel_draft=AsyncMock(),
            list_stories=AsyncMock(return_value=[story()]), get_story=AsyncMock(return_value=story()),
            set_status=AsyncMock(return_value=story(status="paused")), request_check=AsyncMock(return_value="Проверка запущена."),
            recent_updates=AsyncMock(return_value=[change()]), give_feedback=AsyncMock(return_value=True),
            admin_summary=AsyncMock(return_value="Пользователей: 2\nНаблюдений: 4"),
            admin_errors=AsyncMock(return_value="Ошибок нет."), demo_update=AsyncMock(return_value="Тест подготовлен."),
        )
        self.settings = SimpleNamespace(default_check_interval_hours=24, max_stories_per_user=10,
                                        max_manual_checks_per_day=5, manual_check_cooldown_seconds=120,
                                        invite_code="tester_invite", admin_claim_token="SUPER_SECRET_ADMIN_CLAIM")
        self.dispatcher = Dispatcher()
        self.router = build_router(self.service, self.settings)
        self.dispatcher.include_router(self.router)
        self.counter = 1

    async def message(self, text=None, chat_type="private", **fields):
        message = Message(message_id=self.counter, date=NOW,
                          chat=Chat(id=100, type=chat_type),
                          from_user=User(id=100, is_bot=False, first_name="Tester"), text=text, **fields)
        update = Update(update_id=self.counter, message=message)
        self.counter += 1
        await self.dispatcher.feed_update(self.bot, update)

    async def callback(self, data, **message_fields):
        message = Message(message_id=1, date=NOW, chat=Chat(id=100, type="private"), text="Card", **message_fields)
        callback = CallbackQuery(id=str(self.counter), from_user=User(id=100, is_bot=False, first_name="Tester"),
                                 chat_instance="test", data=data, message=message)
        update = Update(update_id=self.counter, callback_query=callback)
        self.counter += 1
        await self.dispatcher.feed_update(self.bot, update)

    @property
    def text(self):
        return "\n".join(getattr(call, "text", "") or "" for call in self.session.calls)

    def reset_throttle(self):
        for middleware in self.router.message.outer_middleware:
            if isinstance(middleware, AccessMiddleware):
                middleware.last_seen.clear()


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"])


def test_notification_untrusted_html_and_links_are_safe():
    rendered = notification_text(
        story(title='<script>alert("x")</script>'),
        change(summary='<a href="tg://user?id=1">click</a>', source_urls=["javascript:alert(1)", "https://example.org/?a=1&b=2"]),
    )
    parser = Tags()
    parser.feed(rendered)
    assert set(parser.tags) <= {"b", "a"}
    assert parser.links == ["https://example.org/?a=1&b=2"]
    assert "&lt;script&gt;" in rendered


def test_cards_stay_below_telegram_utf16_limit():
    hostile = '😀<&"' * 5000
    longest_url = "https://example.org/" + "a" * 510
    current = story(title=hostile, summary=hostile, current_state=hostile, watch_goals=[hostile] * 8)
    update = change(summary=hostile, new_facts=[hostile] * 8, reason=hostile,
                    source_urls=[longest_url + str(i) for i in range(6)], is_demo=True)
    for text in (preview_text(current), story_text(current), notification_text(current, update)):
        assert _units(text) < MAX_MESSAGE_UNITS
        parser = Tags()
        parser.feed(text)
        assert set(parser.tags) <= {"b", "a"}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "tg://user?id=1", "file:///etc/passwd", "https://user:pass@example.org/", "https://example.org:555/", "https://example.org\n/x", "http://[broken", "https://"])
def test_non_web_or_malformed_urls_rejected(url):
    assert safe_url(url) is None


def test_demo_is_explicit_and_sources_not_truncated():
    update = change(is_demo=True, source_urls=["https://example.org/" + "x" * 800])
    text = notification_text(story(), update)
    assert "не реальная новость" in text
    assert "href=" not in text
    keyboard = notification_keyboard(story(), update)
    assert keyboard.inline_keyboard[0][0].url == update.source_urls[0]
    assert any(button.callback_data == "chat:11" for row in keyboard.inline_keyboard for button in row)


@pytest.mark.parametrize("data", [None, "", "unknown:4", "pause:-1", "check:0", "delete_yes:2147483648", "pause:1:2", "pause:1\n", "pause:١"])
def test_malformed_callbacks_rejected(data):
    assert parse_callback(data) is None


@pytest.mark.asyncio
async def test_closed_access_for_text_and_callback():
    harness = Harness(authorized=False)
    await harness.message("Следить за открытием станции")
    await harness.callback("delete_yes:11")
    assert "Telegram ID: 100" in harness.text
    assert len(harness.service.authorize.await_args_list) == 2
    harness.service.prepare_story.assert_not_awaited()
    harness.service.set_status.assert_not_awaited()
    assert any(isinstance(call, AnswerCallbackQuery) and call.show_alert for call in harness.session.calls)


@pytest.mark.asyncio
async def test_groups_are_ignored_before_authorization():
    harness = Harness()
    await harness.message("/start invite_test", chat_type="group")
    harness.service.authorize.assert_not_awaited()
    assert not harness.session.calls


@pytest.mark.asyncio
async def test_start_forwards_invite_and_allows_immediate_first_topic():
    harness = Harness()
    await harness.message("/start invite_test")
    assert harness.service.authorize.await_args.kwargs["start_arg"] == "invite_test"
    await harness.message("Следить за открытием станции метро")
    harness.service.process_user_news.assert_awaited_once()
    assert "Что произошло" in harness.text
    buttons = [button for call in harness.session.calls if isinstance(call, (SendMessage, EditMessageText)) and call.reply_markup
               for row in call.reply_markup.inline_keyboard for button in row]
    assert {button.callback_data for button in buttons} >= {"nwatch:21", "nlater:21", "menu:0"}


@pytest.mark.asyncio
async def test_second_rapid_topic_is_throttled():
    harness = Harness()
    await harness.message("Наблюдать за открытием метро")
    await harness.message("Наблюдать за другим событием")
    assert harness.service.process_user_news.await_count == 1
    assert "Слишком быстро" in harness.text


@pytest.mark.asyncio
async def test_repeated_start_is_throttled_in_its_own_bucket():
    harness = Harness()
    await harness.message("/start")
    await harness.message("/start")
    assert "Слишком быстро" in harness.text
    await harness.message("Хочу следить за открытием станции")
    harness.service.process_user_news.assert_awaited_once()


@pytest.mark.asyncio
async def test_bad_callback_is_acked_without_service_mutation():
    harness = Harness()
    await harness.callback("delete_yes:lol")
    harness.service.set_status.assert_not_awaited()
    harness.service.get_story.assert_not_awaited()
    assert "устарела" in harness.text


@pytest.mark.asyncio
async def test_missing_or_foreign_story_does_not_allow_mutation():
    harness = Harness()
    harness.service.get_story.return_value = None
    await harness.callback("pause:11")
    harness.service.get_story.assert_awaited_once_with(100, 11)
    harness.service.set_status.assert_not_awaited()
    assert "недоступно" in harness.text


@pytest.mark.asyncio
async def test_delete_requires_second_click():
    harness = Harness()
    await harness.callback("delete:11")
    harness.service.set_status.assert_not_awaited()
    assert "нельзя отменить" in harness.text
    harness.reset_throttle()
    await harness.callback("delete_yes:11")
    harness.service.set_status.assert_awaited_once_with(100, 11, "deleted")
    assert any(isinstance(call, EditMessageText) for call in harness.session.calls)


@pytest.mark.asyncio
async def test_confirmation_and_cancel_use_owned_service_operations():
    harness = Harness()
    await harness.callback("watch:11")
    harness.service.confirm_story.assert_awaited_once_with(100, 11)
    harness.reset_throttle()
    await harness.callback("cancel:11")
    harness.service.cancel_draft.assert_awaited_once_with(100, 11)


@pytest.mark.asyncio
async def test_feedback_and_discussion_entry():
    harness = Harness()
    await harness.callback("not_useful:12")
    harness.service.give_feedback.assert_awaited_once_with(100, 12, "not_useful")
    harness.reset_throttle()
    await harness.callback("chat:11")
    assert "/discuss 11" in harness.text and "/account" in harness.text


@pytest.mark.asyncio
async def test_unexpected_exception_is_not_disclosed_or_logged(caplog):
    harness = Harness()
    harness.service.process_user_news.side_effect = RuntimeError("private-api-key-123")
    await harness.message("Следить за запуском нового продукта")
    assert "Не получилось" in harness.text
    assert "private-api-key-123" not in harness.text
    assert "private-api-key-123" not in caplog.text


@pytest.mark.asyncio
async def test_safe_service_error_is_escaped():
    harness = Harness()
    harness.service.request_check.side_effect = UserError("Достигнут лимит <5> проверок")
    await harness.message("/check_now 11")
    assert "&lt;5&gt;" in harness.text
    harness.service.request_check.assert_awaited_once_with(100, 11, progress=ANY, on_complete=ANY)


@pytest.mark.asyncio
async def test_admin_is_guarded_and_invite_never_contains_admin_token():
    harness = Harness()
    await harness.message("/admin")
    harness.service.admin_summary.assert_not_awaited()
    harness.reset_throttle()
    harness.service.is_admin.return_value = True
    await harness.message("/admin")
    assert "https://t.me/news_test_bot?start=invite_tester_invite" in harness.text
    assert "SUPER_SECRET_ADMIN_CLAIM" not in harness.text


@pytest.mark.asyncio
async def test_help_and_disabled_provider_are_candid():
    harness = Harness()
    await harness.message("/help")
    assert "24 ч." in harness.text and "/watching" in harness.text
    harness.reset_throttle()
    harness.service.provider_ready = lambda: False
    harness.service.process_user_news.side_effect = UserError('Нужно подключить API-ключ.')
    await harness.message("Хочу следить за научной миссией")
    assert "API-ключ" in harness.text
    harness.service.save_user_news.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_renders_saved_updates_and_safe_links():
    harness = Harness()
    await harness.callback("history:11")
    harness.service.recent_updates.assert_awaited_once_with(100, 11)
    assert "Последние обновления" in harness.text
    assert "Что нового" in harness.text


@pytest.mark.asyncio
async def test_manual_check_without_id_lists_stories():
    harness = Harness()
    await harness.message("/check_now")
    harness.service.request_check.assert_not_awaited()
    assert "Какое наблюдение проверить" in harness.text
    assert any(button.callback_data == "check:11" for call in harness.session.calls
               if isinstance(call, SendMessage) and call.reply_markup
               for row in call.reply_markup.inline_keyboard for button in row)
