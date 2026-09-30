import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText, SendMessage

from app.ai import AIClient
from app.domain import ProviderUnavailable, SearchResult, StoryExtraction
from app.progress import CheckOutcome, ProgressEvent, progress_context
from app.telegram_progress import TelegramProgress, outcome_text, progress_text
from test_bot import Harness
from test_pipeline import EXTRACTION, Session, completion, settings
from test_service import analysis, setup_service, story


def service_with_results():
    service = setup_service()
    service.repo.get_user.return_value = SimpleNamespace(telegram_id=100)
    service.repo.save_check.return_value = None
    service.repo.finish_check.return_value = True
    service.search.search.return_value = [SearchResult('https://example.org/release', 'Release announced')]
    service.fetcher.fetch.return_value = SimpleNamespace(url='https://example.org/release',
        title='Release announced', text='The company announced a release. ' * 10)
    service.ai.analyze.return_value = analysis(meaningful_update=False)
    return service


async def test_real_stages_precede_the_actual_work_and_final_follows_persistence():
    service = service_with_results()
    events = []
    observed = []

    async def progress(event):
        events.append(event)

    async def search(query):
        assert events[-1].stage == 'searching'
        observed.append('search')
        return [SearchResult('https://example.org/release', 'Release announced')]

    async def fetch(url):
        assert events[-1].stage == 'reading_sources'
        observed.append('fetch')
        return SimpleNamespace(url=url, title='Release', text='New release date confirmed. ' * 10)

    async def analyze(context, candidates):
        assert events[-1].stage == 'analyzing'
        assert events[-1].data['sources'] == len(candidates) == 1
        observed.append('analyze')
        return analysis(meaningful_update=False)

    async def finished(outcome):
        service.repo.finish_check.assert_awaited_once()
        assert outcome.status == 'unchanged' and outcome.sources == 1
        observed.append('finished')

    service.search.search.side_effect = search
    service.fetcher.fetch.side_effect = fetch
    service.ai.analyze.side_effect = analyze
    service.repo.claim_story.return_value = story()
    await service.request_check(100, 10, progress, finished)
    await asyncio.gather(*tuple(service._tasks))
    assert observed == ['search', 'fetch', 'analyze', 'finished']
    assert [event.stage for event in events] == ['queued', 'searching', 'reading_sources', 'analyzing', 'saving_result']
    assert progress_context.get() is None


async def test_no_new_sources_has_explicit_result_without_fake_analysis_stage():
    service = service_with_results()
    service.search.search.return_value = []
    events = []
    async def progress(event):
        events.append(event.stage)
    result = await service.check_story(story(), progress)
    assert result.status == 'no_sources'
    assert 'analyzing' not in events and 'reading_sources' not in events
    service.ai.analyze.assert_not_awaited()
    assert 'Подходящих новых публикаций не найдено' in outcome_text(result, 3)


async def test_unconfirmed_analysis_keeps_articles_eligible_and_does_not_claim_no_news():
    service = service_with_results()
    service.ai.analyze.return_value = analysis(meaningful_update=False, confidence=0)
    result = await service.check_story(story())
    assert result.status == 'unverified'
    assert service.repo.save_check.call_args.args[2] == []
    rendered = outcome_text(result, 10)
    assert 'Это не означает, что развития нет' in rendered
    assert 'полные тексты недоступны' not in rendered
    assert 'Полных текстов: 1' in rendered


async def test_partial_search_is_disclosed_and_total_failure_is_not_no_news():
    service = service_with_results()
    item = story()
    item.search_queries = ['first', 'second']
    service.search.search.side_effect = [OSError('private-key'), []]
    result = await service.check_story(item)
    assert result.status == 'no_sources' and result.partial_search
    assert 'неполный' in outcome_text(result, 4)
    service.search.search.side_effect = OSError('private-key')
    result = await service.check_story(item)
    rendered = outcome_text(result, 4)
    assert result.status == 'error'
    assert 'не завершена' in rendered and 'private-key' not in rendered and '✅' not in rendered


@pytest.mark.parametrize('finish_result,expected', [(False, 'cancelled'), (True, 'unchanged')])
async def test_stale_or_paused_check_cannot_report_success(finish_result, expected):
    service = service_with_results()
    service.repo.finish_check.return_value = finish_result
    result = await service.check_story(story())
    assert result.status == expected


async def test_finish_storage_failure_reports_error():
    service = service_with_results()
    service.repo.finish_check.side_effect = OSError('private-db-url')
    result = await service.check_story(story())
    assert result.status == 'error'
    assert 'private-db-url' not in outcome_text(result, 3)


async def test_meaningful_result_points_to_existing_outbox_update():
    service = service_with_results()
    service.repo.save_check.return_value = SimpleNamespace(id=42)
    service.ai.analyze.return_value = analysis()
    result = await service.check_story(story())
    assert result.status == 'changed'
    assert 'отдельном уведомлении' in outcome_text(result, 5)


async def test_shutdown_completes_manual_status_and_releases_lease():
    service = service_with_results()
    entered = asyncio.Event()
    async def search(query):
        entered.set()
        await asyncio.Event().wait()
    service.search.search.side_effect = search
    service.repo.claim_story.return_value = story()
    done = AsyncMock()
    await service.request_check(100, 10, on_complete=done)
    await entered.wait()
    await service.close()
    done.assert_awaited_once()
    assert done.call_args.args[0].status == 'cancelled'
    service.repo.finish_check.assert_awaited_once_with(10, 'test-lease', error=True)


async def test_progress_callback_failure_cannot_break_the_pipeline(caplog):
    service = service_with_results()
    broken = AsyncMock(side_effect=RuntimeError('secret-from-ui'))
    result = await service.check_story(story(), broken)
    assert result.status == 'unchanged'
    assert 'secret-from-ui' not in caplog.text


async def test_concurrent_progress_is_isolated_and_background_check_stays_silent():
    service = service_with_results()
    service.search.search.return_value = []
    one, two = AsyncMock(), AsyncMock()
    a, b = story(), story()
    b.id = 11
    b.search_queries = ['x', 'y']
    await asyncio.gather(service.check_story(a, one), service.check_story(b, two))
    assert len([call for call in one.call_args_list if call.args[0].stage == 'searching']) == 1
    assert len([call for call in two.call_args_list if call.args[0].stage == 'searching']) == 2
    stray = AsyncMock()
    token = progress_context.set(stray)
    try:
        await service.check_story(a)
        stray.assert_not_called()
    finally:
        progress_context.reset(token)


async def test_preparation_reports_read_extract_save_only_when_performed():
    service = service_with_results()
    service.ai.extract.return_value = StoryExtraction(**EXTRACTION)
    events = []
    async def progress(event):
        events.append(event.stage)
    await service.prepare_story(100, 'https://example.org/release', progress)
    assert events == ['reading_input', 'extracting', 'saving_draft']
    events.clear()
    await service.prepare_story(100, 'Следить за запуском продукта', progress)
    assert events == ['extracting', 'saving_draft']
    assert progress_context.get() is None


async def test_model_retry_event_matches_actual_request_attempt():
    client = AIClient(settings())
    client._session = Session([completion({'title': 'Incomplete'}), completion(EXTRACTION)])
    events = []
    async def progress(event):
        # Event is emitted just before its corresponding HTTP attempt.
        assert len(client._session.calls) == event.data['attempt'] - 1
        events.append(event)
    token = progress_context.set(progress)
    try:
        await client.extract('Follow this product launch')
    finally:
        progress_context.reset(token)
    assert [event.data['attempt'] for event in events] == [1, 2]


async def test_rejected_budget_never_claims_model_request_started():
    client = AIClient(settings(), AsyncMock(side_effect=ProviderUnavailable('Daily limit')))
    client._session = Session([])
    events = AsyncMock()
    token = progress_context.set(events)
    try:
        with pytest.raises(ProviderUnavailable):
            await client.extract('Follow this product launch')
    finally:
        progress_context.reset(token)
    events.assert_not_awaited()


async def test_single_status_message_becomes_draft_card_in_real_router():
    harness = Harness()
    await harness.message('Следить за открытием новой станции')
    sends = [call for call in harness.session.calls if isinstance(call, SendMessage)]
    edits = [call for call in harness.session.calls if isinstance(call, EditMessageText)]
    assert len(sends) == 1 and len(edits) == 1
    assert edits[0].message_id == 99 and 'Что произошло' in edits[0].text
    assert edits[0].reply_markup


@pytest.mark.parametrize('trigger', ['command', 'button'])
async def test_manual_ui_shows_final_even_without_news(trigger):
    harness = Harness()
    async def request(user_id, story_id, progress, on_complete):
        await progress(ProgressEvent('searching', {'current': 1, 'total': 2}))
        await on_complete(CheckOutcome('no_sources'))
    harness.service.request_check.side_effect = request
    if trigger == 'command':
        await harness.message('/check_now 11')
    else:
        await harness.callback('check:11')
    sends = [call for call in harness.session.calls if isinstance(call, SendMessage)]
    edits = [call for call in harness.session.calls if isinstance(call, EditMessageText)]
    assert len(sends) == 1 and len(edits) == 1
    assert 'Проверка завершена' in edits[-1].text
    assert 'story:11' in str(edits[-1].reply_markup)


async def test_real_elapsed_updates_while_same_operation_waits_then_stops():
    message = SimpleNamespace(edit_text=AsyncMock())
    times = [0.0]
    display = TelegramProgress(message, 'check', 11, clock=lambda: times[0],
                               refresh_seconds=0.01, min_edit_seconds=0)
    display.start()
    await display.update(ProgressEvent('searching', {'current': 1, 'total': 2}))
    await asyncio.sleep(0.02)
    times[0] = 40
    await asyncio.sleep(0.025)
    assert any('40 с' in call.args[0] and 'Текущий этап ещё выполняется' in call.args[0]
               for call in message.edit_text.call_args_list)
    await display.complete(CheckOutcome('no_sources'))
    count = message.edit_text.await_count
    await display.update(ProgressEvent('analyzing', {'sources': 5}))
    await asyncio.sleep(0.02)
    assert message.edit_text.await_count == count
    assert 'Проверка завершена' in message.edit_text.call_args.args[0]
    assert display._task.done()


async def test_terminal_result_retries_transient_failure_without_new_message(monkeypatch):
    monkeypatch.setattr('app.telegram_progress.ERROR_RETRY_SECONDS', 0.01)
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=[OSError('secret-url'), True]))
    display = TelegramProgress(message, 'check', 11, refresh_seconds=0.01)
    display.start()
    await display.complete(CheckOutcome('no_sources'))
    assert message.edit_text.await_count == 2
    assert display._terminal_task.done()


async def test_telegram_flood_wait_is_respected_for_terminal_result():
    method = EditMessageText(chat_id=100, message_id=1, text='Status')
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=[
        TelegramRetryAfter(method=method, message='Flood control', retry_after=0.02), True]))
    display = TelegramProgress(message, 'check', 11)
    await display.complete(CheckOutcome('unchanged', sources=2))
    assert message.edit_text.await_count == 2


async def test_unresponsive_telegram_edit_has_a_hard_timeout(monkeypatch):
    monkeypatch.setattr('app.telegram_progress.EDIT_TIMEOUT', 0.01)
    async def stuck(*args, **kwargs):
        await asyncio.Event().wait()
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=stuck))
    display = TelegramProgress(message, 'check', 11)
    async with asyncio.timeout(0.5):
        await display._edit('Current stage')
    assert display._last_text is None and display._retry_at > display.clock()


def test_rendering_has_no_fake_percent_and_escapes_safe_error_text():
    text = progress_text('check', 11, ProgressEvent('model_wait', {'purpose': 'analyze', 'attempt': 2, 'sources': 3}), 71, 30)
    assert 'повторный запрос' in text and '1 мин 11 с' in text and '%' not in text
    text = outcome_text(CheckOutcome('error', message='<private>'), 1)
    assert '&lt;private&gt;' in text and '<private>' not in text
