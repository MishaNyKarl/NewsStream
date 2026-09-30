"""Bounded UI traffic, independent of expensive news/LLM work."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import EditMessageText

from app.progress import CheckOutcome, ProgressEvent
from app.telegram_progress import ProgressEditBudget, TelegramProgress


async def test_real_seconds_tick_without_stage_changes_and_stop_on_completion():
    ticks = []
    ready = asyncio.Event()
    async def edit(text, **kwargs):
        if 'Прошло:' in text:
            ticks.append((time.monotonic(), text))
            if len(ticks) == 3:
                ready.set()
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=edit))
    display = TelegramProgress(message, 'check', 11)
    display.start()
    try:
        await display.update(ProgressEvent('model_wait', {'purpose': 'analyze'}))
        await asyncio.wait_for(ready.wait(), timeout=6)
    finally:
        await display.complete(CheckOutcome('no_sources'))
    assert all(b[0] - a[0] >= 0.98 for a, b in zip(ticks, ticks[1:]))
    assert all(f'Прошло: {i} с' in item[1] for i, item in enumerate(ticks, 1))
    assert all('Жду' not in item[1] and 'жду ответ модели' in item[1] for item in ticks)
    count = message.edit_text.await_count
    await asyncio.sleep(1.1)
    assert message.edit_text.await_count == count
    assert 'Проверка завершена' in message.edit_text.call_args.args[0]


def test_budget_caps_chat_and_aggregate_and_drops_expired_chat_entries():
    now = [0.0]
    budget = ProgressEditBudget(clock=lambda: now[0])
    assert budget.retry_delay(1) == 0
    assert budget.retry_delay(1) == 1
    assert budget.retry_delay(2) == 0.1
    now[0] = 0.11
    assert budget.retry_delay(2) == 0
    assert budget.retry_delay(1) > 0.8
    now[0] = 3
    assert budget.retry_delay(3) == 0
    assert set(budget.chats) == {3}


async def test_simultaneous_statuses_share_budget_and_skip_intermediate_tick():
    now = [10.0]
    budget = ProgressEditBudget(clock=lambda: now[0])
    messages = [SimpleNamespace(chat=SimpleNamespace(id=100), edit_text=AsyncMock()) for _ in range(2)]
    displays = [TelegramProgress(m, 'check', i, clock=lambda: now[0], budget=budget)
                for i, m in enumerate(messages)]
    await asyncio.gather(*(d._edit('First tick') for d in displays))
    assert sum(m.edit_text.await_count for m in messages) == 1
    now[0] += 1.1
    await displays[1]._edit('Latest tick')
    assert messages[1].edit_text.call_args.args[0] == 'Latest tick'
    assert messages[1].edit_text.await_count == 1


async def test_telegram_flood_pauses_other_timers_and_slows_current_operation():
    now = [10.0]
    budget = ProgressEditBudget(clock=lambda: now[0])
    method = EditMessageText(chat_id=100, message_id=1, text='Status')
    failure = TelegramRetryAfter(method=method, message='Flood control', retry_after=7)
    message = SimpleNamespace(chat=SimpleNamespace(id=100), edit_text=AsyncMock(side_effect=failure))
    display = TelegramProgress(message, 'check', clock=lambda: now[0], budget=budget)
    await display._edit('Current tick')
    assert display._retry_at == 17
    assert display.refresh_seconds == display.min_edit_seconds == 5
    assert budget.retry_delay(200) == 7
    now[0] = 17.1
    assert budget.retry_delay(200) == 0


async def test_waiting_for_shared_slot_does_not_exhaust_terminal_retries():
    class BusyBudget:
        skips = 0
        def retry_delay(self, chat_id):
            self.skips += 1
            return 0.001 if self.skips <= 8 else 0
    message = SimpleNamespace(edit_text=AsyncMock())
    display = TelegramProgress(message, 'check', budget=BusyBudget())
    await display.complete(CheckOutcome('no_sources'))
    assert message.edit_text.await_count == 1
    assert display._terminal_task.done()


async def test_network_error_cannot_trigger_one_request_every_second():
    now = [10.0]
    message = SimpleNamespace(edit_text=AsyncMock(side_effect=OSError('offline')))
    display = TelegramProgress(message, 'check', clock=lambda: now[0])
    await display._edit('Current tick')
    assert display._retry_at >= 15


async def test_concurrent_timers_get_frequent_ticks_instead_of_missing_shared_slots():
    budget = ProgressEditBudget()
    messages = [SimpleNamespace(chat=SimpleNamespace(id=i), edit_text=AsyncMock()) for i in range(3)]
    displays = [TelegramProgress(m, 'check', i, budget=budget) for i, m in enumerate(messages)]
    try:
        for display in displays:
            display.start()
            await display.update(ProgressEvent('analyzing', {'sources': 2}))
        await asyncio.sleep(2.6)
        assert all(m.edit_text.await_count >= 2 for m in messages)
    finally:
        await asyncio.gather(*(d.complete(CheckOutcome('no_sources')) for d in displays))
