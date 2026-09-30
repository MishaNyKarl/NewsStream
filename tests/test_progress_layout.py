from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, MessageOriginChannel, User

from app.bot import MAX_MESSAGE_UNITS, _units, preview_text
from app.domain import UserError
from app.progress import CheckOutcome, ProgressEvent
from app.telegram_progress import TelegramProgress, progress_text, time_footer
from test_bot import Harness, NOW, Tags, story


async def test_waiting_changes_elapsed_time_but_never_advances_the_bar():
    start = datetime(2026, 9, 30, 21, 59, 58, tzinfo=timezone.utc)
    event = ProgressEvent('reading_sources', {'current': 2, 'results': 5})
    before = progress_text('check', 11, event, 2, 0, started_at=start, visited={0, 1})
    after = progress_text('check', 11, event, 71, 69, started_at=start, visited={0, 1})
    bar = '<code>■■■ ▸▸▸ □□□ □□□</code>'
    assert bar in before and bar in after
    assert 'Прошло: 2 с' in before and 'Прошло: 1 мин 11 с' in after
    assert 'Старт: 01.10.2026 · 00:59:58 МСК' in before
    assert 'Старт: 01.10.2026 · 00:59:58 МСК' in after
    assert 'материал №2' in after and '2/4' in after


async def test_no_sources_marks_unneeded_reading_and_analysis_as_skipped():
    message = SimpleNamespace(edit_text=AsyncMock())
    display = TelegramProgress(message, 'check', 11)
    await display.update(ProgressEvent('searching', {'current': 1, 'total': 1}))
    await display.update(ProgressEvent('saving_result', {'sources': 0}))
    await display.complete(CheckOutcome('no_sources'))
    text = message.edit_text.call_args.args[0]
    assert '<code>■■■ ─── ─── ■■■</code>' in text
    assert 'этап не понадобился' in text and 'Подходящих новых публикаций не найдено' in text
    assert 'Заняло:' in text and 'МСК' in text and '→' in text


@pytest.mark.parametrize('status', ['error', 'cancelled'])
async def test_failed_or_cancelled_analysis_never_shows_completed_bar(status):
    message = SimpleNamespace(edit_text=AsyncMock())
    display = TelegramProgress(message, 'check', 11)
    for stage in ('searching', 'reading_sources', 'analyzing'):
        await display.update(ProgressEvent(stage))
    await display.complete(CheckOutcome(status, message='<Ошибка>'))
    text = message.edit_text.call_args.args[0]
    assert '<code>■■■ ■■■ ××× □□□</code>' in text and 'Готово' not in text
    assert '&lt;Ошибка&gt;' in text and '<Ошибка>' not in text


def test_final_time_range_handles_moscow_midnight():
    start = datetime(2026, 9, 30, 20, 59, 55, tzinfo=timezone.utc)
    end = datetime(2026, 9, 30, 21, 0, 10, tzinfo=timezone.utc)
    text = time_footer(15, start, end)
    assert 'Заняло: 15 с' in text
    assert '30.09.2026 · 23:59:55 → 01.10.2026 · 00:00:10 МСК' in text


@pytest.mark.parametrize('forwarded', [False, True])
async def test_creation_replies_to_user_post_not_public_channel_post(forwarded):
    harness = Harness()
    fields = {'caption': 'Объявлена дата запуска новой космической станции.'} if forwarded else {}
    if forwarded:
        fields['forward_origin'] = MessageOriginChannel(
            date=NOW, chat=Chat(id=-100123, type='channel', title='Новости', username='example_news'),
            message_id=987)
    await harness.message(None if forwarded else 'Следить за запуском космической станции', **fields)
    sends = [call for call in harness.session.calls if isinstance(call, SendMessage)]
    edits = [call for call in harness.session.calls if isinstance(call, EditMessageText)]
    assert len(sends) == len(edits) == 1
    assert sends[0].reply_parameters.message_id == 1
    assert sends[0].reply_parameters.allow_sending_without_reply is True
    assert '<code>' in sends[0].text and 'Старт:' in sends[0].text
    assert 'Что произошло' in edits[0].text and 'МСК' in edits[0].text


@pytest.mark.parametrize('trigger,expected', [('command', 1), ('card', 1), ('reply_card', 42)])
async def test_manual_check_replies_to_command_or_original_user_message(trigger, expected):
    harness = Harness()
    async def request(user_id, story_id, progress, on_complete):
        await progress(ProgressEvent('searching'))
        await progress(ProgressEvent('saving_result'))
        await on_complete(CheckOutcome('no_sources'))
    harness.service.request_check.side_effect = request
    if trigger == 'command':
        await harness.message('/check_now 11')
    else:
        fields = {}
        if trigger == 'reply_card':
            fields = dict(from_user=User(id=777, is_bot=True, first_name='Bot'), reply_to_message=Message(
                message_id=42, date=NOW, chat=Chat(id=100, type='private'), text='Исходная новость',
                from_user=User(id=100, is_bot=False, first_name='Tester')))
        await harness.callback('check:11', **fields)
    sends = [call for call in harness.session.calls if isinstance(call, SendMessage)]
    edits = [call for call in harness.session.calls if isinstance(call, EditMessageText)]
    assert len(sends) == len(edits) == 1
    assert sends[0].reply_parameters.message_id == expected
    assert sends[0].reply_parameters.allow_sending_without_reply is True
    assert 'Проверка темы №11' in edits[0].text and edits[0].message_id == 99


async def test_rejected_check_has_terminal_time_and_no_completed_stages():
    harness = Harness()
    harness.service.request_check.side_effect = UserError('Проверка уже идёт.')
    await harness.message('/check_now 11')
    edit = next(call for call in harness.session.calls if isinstance(call, EditMessageText))
    assert 'Проверка уже идёт' in edit.text and 'Не завершено' in edit.text
    assert 'Заняло:' in edit.text and '■■■' not in edit.text


async def test_maximum_draft_with_bar_and_dates_fits_telegram_html_limit():
    hostile = '😀<&"' * 5000
    draft = story(title=hostile, summary=hostile, watch_goals=[hostile] * 8)
    message = SimpleNamespace(edit_text=AsyncMock())
    display = TelegramProgress(message, 'prepare')
    await display.update(ProgressEvent('extracting'))
    await display.update(ProgressEvent('saving_draft'))
    await display.finish(preview_text(draft))
    text = message.edit_text.call_args.args[0]
    assert _units(text) < MAX_MESSAGE_UNITS
    assert '<code>■■■ ■■■ ■■■</code> · Готово' in text
    parser = Tags()
    parser.feed(text)
    assert set(parser.tags) <= {'b', 'a', 'code'}
