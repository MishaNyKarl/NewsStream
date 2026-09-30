"""One editable Telegram message with real stages and elapsed wall time."""
import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

from app.progress import ProgressEvent

log = logging.getLogger(__name__)
EDIT_TIMEOUT = 4
ERROR_RETRY_SECONDS = 5
# Keep terminal retries alive without occupying analysis slots. They only edit
# an existing message, expire after two minutes and never trigger another check.
_terminal_tasks: set[asyncio.Task] = set()
MSK = timezone(timedelta(hours=3))


class ProgressEditBudget:
    """Shared by a router: at most one progress edit/chat/s and ten overall/s.

    Admission has no await, so concurrent tasks on the bot loop cannot reserve
    the same slot. Intermediate ticks are coalesced, never queued for replay.
    This budget covers progress edits only, leaving room for other bot traffic.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.next_global = 0
        self.blocked_until = 0
        self.chats = {}

    def retry_delay(self, chat_id):
        now = self.clock()
        delay = max(self.next_global, self.blocked_until, self.chats.get(chat_id, 0)) - now
        if delay > 0:
            return delay
        self.chats = {key: due for key, due in self.chats.items() if due > now}
        self.chats[chat_id] = now + 1
        self.next_global = now + 0.1
        return 0

    def pause(self, seconds):
        self.blocked_until = max(self.blocked_until, self.clock() + seconds)


def stage_index(operation, event):
    if operation == "check":
        return {"searching": 0, "reading_sources": 1, "analyzing": 2, "verifying": 2,
                "model_wait": 2, "saving_result": 3}.get(event.stage)
    return {"starting": 0, "reading_input": 0, "extracting": 1,
            "model_wait": 1, "saving_draft": 2}.get(event.stage)


def stage_bar(operation, event, visited=None, status=None):
    """A work-stage indicator, never an estimate of time or percentage.

    A dash means a stage was skipped. An error leaves later stages empty.
    Only observed stages can become completed segments.
    """
    count = 4 if operation == "check" else 3
    current = stage_index(operation, event)
    seen = set(visited or ())
    if current is not None:
        seen.add(current)
    successful = status in {"completed", "changed", "unchanged", "no_sources", "unverified"}
    segments = []
    for index in range(count):
        if status and not successful and index == current:
            segment = "×××"
        elif status is None and index == current:
            segment = "▸▸▸"
        elif index in seen:
            segment = "■■■"
        elif successful or (current is not None and index < current):
            segment = "───"
        else:
            segment = "□□□"
        segments.append(segment)
    if status:
        label = ("Готово" if successful else "Остановлено" if status == "cancelled" else "Не завершено")
    else:
        label = f"{current + 1}/{count}" if current is not None else "Подготовка"
    line = f"<code>{' '.join(segments)}</code> · {label}"
    if "───" in segments:
        line += "\n─ этап не понадобился"
    return line


def time_footer(elapsed, started_at=None, finished_at=None):
    label = "Заняло" if finished_at else "Прошло"
    text = f"⏱ {label}: {elapsed_text(elapsed)}"
    if started_at is not None:
        start = started_at.astimezone(MSK)
        if finished_at is None:
            text += f"\n🗓 Старт: {start:%d.%m.%Y · %H:%M:%S} МСК"
        else:
            end = finished_at.astimezone(MSK)
            end_text = end.strftime("%H:%M:%S" if start.date() == end.date() else "%d.%m.%Y · %H:%M:%S")
            text += f"\n🗓 {start:%d.%m.%Y · %H:%M:%S} → {end_text} МСК"
    return text


def elapsed_text(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} мин {seconds:02d} с" if minutes else f"{seconds} с"


def progress_text(operation, story_id, event, elapsed, stage_elapsed, *, started_at=None, visited=None):
    heading = f"Проверяю тему №{story_id}" if operation == "check" else "Готовлю наблюдение"
    data = event.data
    stages = {
        "starting": "⏳ Запускаю проверку…" if operation == "check" else "⏳ Принимаю новость…",
        "queued": "⏳ Подготавливаю проверку…",
        "reading_input": "📄 Открываю ссылку и читаю текст новости…",
        "extracting": "🧠 Разбираю событие и определяю, за чем следить…",
        "saving_draft": "💾 Сохраняю карточку наблюдения…",
        "searching": f"🔎 Ищу публикации — запрос {data.get('current', 1)} из {data.get('total', 1)}…",
        "reading_sources": f"📄 Изучаю источники — материал №{data.get('current', 1)}…",
        "analyzing": f"🧠 Сравниваю новые материалы с известными фактами: {data.get('sources', 0)}…",
        "verifying": "🔎 Проверяю формулировки и подтверждения в источниках…",
        "saving_result": "💾 Сохраняю результат проверки…",
    }
    if event.stage == "model_wait":
        action = {"extract": "Разбираю новость", "verify": "Проверяю подтверждения"}.get(data.get("purpose"), "Сравниваю факты")
        stage = f"🧠 {action} — жду ответ модели…"
        if data.get("attempt", 1) > 1:
            stage += "\nПервая попытка не дала корректного результата. Выполняю повторный запрос."
    else:
        stage = stages.get(event.stage, "⏳ Выполняю проверку…")
    details = ""
    if event.stage == "reading_sources":
        details = f"\nПолучено результатов поиска: {data.get('results', 0)}. Отбираю новые материалы."
    elif event.stage == "model_wait" and data.get("purpose") == "analyze":
        details = f"\nНовых материалов для анализа: {data.get('sources', 0)}."
    if stage_elapsed >= 30:
        details += "\nТекущий этап ещё выполняется."
    bar = stage_bar(operation, event, visited)
    return f"<b>{heading}</b>\n{bar}\n\n{stage}{details}\n\n{time_footer(elapsed, started_at)}"


def outcome_text(outcome, elapsed=None):
    if outcome.status == "changed":
        finding = "Найдено уточнение исходной новости." if outcome.update_kind == "context" else "Найдено существенное развитие."
        body = f"✅ Проверка завершена. {finding}\nПодробности — в отдельном уведомлении и истории темы."
    elif outcome.status == "unchanged":
        body = "✅ Проверка завершена. Существенного развития не найдено в проверенных материалах."
    elif outcome.status == "no_sources":
        body = "✅ Проверка завершена. Подходящих новых публикаций не найдено в проверенных источниках."
    elif outcome.status == "unverified":
        body = "⚠️ Материалы найдены, но надёжно подтвердить развитие пока не удалось. Это не означает, что развития нет."
        if outcome.search_summary.get('full_texts', 0) == 0:
            body += "\nНайдены только заголовки и фрагменты; полные тексты недоступны."
    elif outcome.status == "cancelled":
        body = "⏹ " + html.escape(outcome.message[:1800])
    else:
        body = "⚠️ " + html.escape(outcome.message[:1800]) + "\nЭта проверка не завершена; отсутствие новых фактов не подтверждено."
    if outcome.status in {"changed", "unchanged"}:
        body += f"\nМатериалов для анализа: {outcome.sources}."
    summary = outcome.search_summary
    if summary:
        labels = {'bing_news': 'Bing News', 'google_news': 'Google News', 'hybrid_news': 'Bing News + Google News'}
        providers = ', '.join(labels.get(value, value) for value in summary.get('providers', []))
        if providers:
            body += f"\n\n🔎 {html.escape(providers)} · запросов: {summary.get('queries', 0)}"
        body += f"\nРезультатов выдачи: {summary.get('results', 0)}."
        body += f"\nПолных текстов: {summary.get('full_texts', 0)} · фрагментов: {summary.get('snippets', 0)}."
        if outcome.status == "no_sources":
            body += f"\nПовторы: {summary.get('duplicates', 0)} · вне периода поиска: {summary.get('outside_window', 0)}."
    if outcome.partial_search:
        body += "\nЧасть поисковых запросов была недоступна — результат неполный."
    return body + (f"\n\nВремя: {elapsed_text(elapsed)}" if elapsed is not None else "")


class TelegramProgress:
    def __init__(self, message, operation, story_id=None, clock=time.monotonic,
                 refresh_seconds=1, min_edit_seconds=1, started_at=None, budget=None):
        self.message = message
        self.operation = operation
        self.story_id = story_id
        self.clock = clock
        self.started = self.stage_started = clock()
        self.started_at = started_at or datetime.now(timezone.utc)
        self.event = ProgressEvent("starting")
        self.visited = {0} if operation == "prepare" else set()
        self.refresh_seconds = refresh_seconds
        self.min_edit_seconds = min_edit_seconds
        self.budget = budget
        self._chat_id = getattr(getattr(message, 'chat', None), 'id', None)
        self._wake = asyncio.Event()
        self._task = None
        self._closed = False
        self._unavailable = False
        self._last_text = None
        self._retry_at = 0
        self._terminal_task = None

    @classmethod
    async def begin(cls, message, operation, story_id=None, *, budget=None):
        display = cls(message, operation, story_id, budget=budget)
        text = progress_text(operation, story_id, display.event, 0, 0,
                             started_at=display.started_at, visited=display.visited)
        # When a button belongs to a reply card, preserve the original user's
        # message as the anchor. Otherwise reply to the command/post/card itself.
        target = message
        original = message.reply_to_message
        if (message.from_user and message.from_user.is_bot and original
                and original.from_user and not original.from_user.is_bot
                and original.chat.id == message.chat.id):
            target = original
        display.message = await message.answer(
            text, parse_mode="HTML", reply_markup=display.keyboard(), disable_web_page_preview=True,
            reply_parameters=ReplyParameters(message_id=target.message_id, allow_sending_without_reply=True))
        display._last_text = text
        display.start()
        return display

    def keyboard(self):
        if self.story_id is None:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Открыть тему", callback_data=f"story:{self.story_id}")]])

    def start(self):
        self._task = asyncio.create_task(self._run(), name="telegram-progress")

    async def update(self, event):
        if self._closed:
            return
        if event.stage != self.event.stage:
            self.stage_started = self.clock()
        self.event = ProgressEvent(event.stage, {**self.event.data, **event.data})
        index = stage_index(self.operation, self.event)
        if index is not None:
            self.visited.add(index)
        self._wake.set()

    async def _edit(self, text, keyboard=None):
        if self._unavailable or text == self._last_text:
            return False
        if self.budget is not None:
            delay = self.budget.retry_delay(self._chat_id)
            if delay > 0:
                self._retry_at = max(self._retry_at, self.clock() + delay)
                return False
        try:
            # Message shortcuts pass extra keywords into the API body; they do
            # not accept Bot's request_timeout argument. Bound the await itself.
            async with asyncio.timeout(EDIT_TIMEOUT):
                await self.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard,
                                             disable_web_page_preview=True)
            self._last_text = text
        except TelegramRetryAfter as exc:
            self._retry_at = self.clock() + exc.retry_after
            # Keep this operation slow after a flood response, even when stages
            # change rapidly. Other live timers also respect the requested pause.
            self.refresh_seconds = max(self.refresh_seconds, 5)
            self.min_edit_seconds = max(self.min_edit_seconds, 5)
            if self.budget is not None:
                self.budget.pause(exc.retry_after)
        except TelegramForbiddenError:
            self._unavailable = True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                self._last_text = text
            else:
                self._unavailable = True
        except Exception as exc:
            self._retry_at = self.clock() + max(ERROR_RETRY_SECONDS, self.refresh_seconds)
            log.warning("progress_edit_failed error_type=%s", type(exc).__name__)
        return True

    async def _deliver_terminal(self, text, keyboard):
        try:
            async with asyncio.timeout(120):
                attempts = 0
                while attempts < 5:
                    if self._unavailable or self._last_text == text:
                        return
                    delay = self._retry_at - self.clock()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    # Waiting for a shared slot is not a failed delivery attempt.
                    attempts += bool(await self._edit(text, keyboard))
        except TimeoutError:
            log.warning("terminal_progress_delivery_expired")

    async def _run(self):
        loop = asyncio.get_running_loop()
        next_edit = loop.time() + self.min_edit_seconds
        next_tick = loop.time() + self.refresh_seconds
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=max(0, next_tick - loop.time()))
            except TimeoutError:
                pass
            self._wake.clear()
            delay = max(next_edit - loop.time(), self._retry_at - self.clock())
            if delay > 0:
                # A terminal result wakes this wait immediately.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                if not self._closed and (loop.time() < next_edit or self.clock() < self._retry_at):
                    continue
            if self._closed or self._unavailable:
                return
            text = progress_text(self.operation, self.story_id, self.event,
                                 self.clock() - self.started, self.clock() - self.stage_started,
                                 started_at=self.started_at, visited=self.visited)
            attempt_started = loop.time()
            if await self._edit(text, self.keyboard()):
                next_edit = attempt_started + self.min_edit_seconds
                next_tick = attempt_started + self.refresh_seconds
            else:
                # A busy shared slot is not an edit. Try when it opens instead
                # of synchronizing all waiting timers onto another full second.
                retry_delay = self._retry_at - self.clock()
                next_tick = loop.time() + (retry_delay if retry_delay > 0 else self.refresh_seconds)

    async def finish(self, text, keyboard=None, *, status="completed"):
        if self._closed:
            return
        heading = f"<b>Проверка темы №{self.story_id}</b>\n" if self.operation == "check" else ""
        bar = stage_bar(self.operation, self.event, self.visited, status)
        footer = time_footer(self.clock() - self.started, self.started_at, datetime.now(timezone.utc))
        text = f"{heading}{bar}\n\n{text}\n\n{footer}"
        self._closed = True
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=6)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        self._terminal_task = asyncio.create_task(self._deliver_terminal(text, keyboard), name="telegram-progress-result")
        _terminal_tasks.add(self._terminal_task)
        self._terminal_task.add_done_callback(_terminal_tasks.discard)
        try:
            # If Telegram asks us to wait, delivery continues independently.
            await asyncio.wait_for(asyncio.shield(self._terminal_task), timeout=5)
        except TimeoutError:
            pass

    async def complete(self, outcome):
        await self.finish(outcome_text(outcome), self.keyboard(), status=outcome.status)
