"""One editable Telegram message with real stages and elapsed wall time."""
import asyncio
import html
import logging
import time

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.progress import ProgressEvent

log = logging.getLogger(__name__)
EDIT_TIMEOUT = 4
# Keep terminal retries alive without occupying analysis slots. They only edit
# an existing message, expire after two minutes and never trigger another check.
_terminal_tasks: set[asyncio.Task] = set()


def elapsed_text(seconds):
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} мин {seconds:02d} с" if minutes else f"{seconds} с"


def progress_text(operation, story_id, event, elapsed, stage_elapsed):
    heading = f"Проверяю тему №{story_id}" if operation == "check" else "Готовлю наблюдение"
    data = event.data
    stages = {
        "starting": "⏳ Запускаю проверку…" if operation == "check" else "⏳ Принимаю новость…",
        "queued": "⏳ Подготавливаю проверку…",
        "reading_input": "📄 Открываю ссылку и читаю текст новости…",
        "extracting": "🧠 Разбираю событие и определяю, за чем следить…",
        "saving_draft": "💾 Сохраняю карточку наблюдения…",
        "searching": f"🔎 Ищу публикации — запрос {data.get('current', 1)} из {data.get('total', 1)}…",
        "reading_sources": f"📄 Читаю источники — открываю материал №{data.get('current', 1)}…",
        "analyzing": f"🧠 Сравниваю новые материалы с известными фактами: {data.get('sources', 0)}…",
        "saving_result": "💾 Сохраняю результат проверки…",
    }
    if event.stage == "model_wait":
        action = "Разбираю новость" if data.get("purpose") == "extract" else "Сравниваю факты"
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
    return f"<b>{heading}</b>\n\n{stage}{details}\n\nПрошло: {elapsed_text(elapsed)}"


def outcome_text(outcome, elapsed):
    if outcome.status == "changed":
        body = "✅ Проверка завершена. Найдено существенное развитие.\nПодробности — в отдельном уведомлении и истории темы."
    elif outcome.status == "unchanged":
        body = "✅ Проверка завершена. Существенного развития не найдено в проверенных материалах."
    elif outcome.status == "no_sources":
        body = "✅ Проверка завершена. Подходящих новых публикаций не найдено в проверенных источниках."
    elif outcome.status == "cancelled":
        body = "⏹ " + html.escape(outcome.message[:1800])
    else:
        body = "⚠️ " + html.escape(outcome.message[:1800]) + "\nЭта проверка не завершена; отсутствие новых фактов не подтверждено."
    if outcome.status in {"changed", "unchanged"}:
        body += f"\nНовых материалов проверено: {outcome.sources}."
    if outcome.partial_search:
        body += "\nЧасть поисковых запросов была недоступна — результат неполный."
    return body + f"\n\nВремя: {elapsed_text(elapsed)}"


class TelegramProgress:
    def __init__(self, message, operation, story_id=None, clock=time.monotonic,
                 refresh_seconds=10, min_edit_seconds=2):
        self.message = message
        self.operation = operation
        self.story_id = story_id
        self.clock = clock
        self.started = self.stage_started = clock()
        self.event = ProgressEvent("starting")
        self.refresh_seconds = refresh_seconds
        self.min_edit_seconds = min_edit_seconds
        self._wake = asyncio.Event()
        self._task = None
        self._closed = False
        self._unavailable = False
        self._last_text = None
        self._retry_at = 0
        self._terminal_task = None

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
        self._wake.set()

    async def _edit(self, text, keyboard=None):
        if self._unavailable or text == self._last_text:
            return
        try:
            # Message shortcuts pass extra keywords into the API body; they do
            # not accept Bot's request_timeout argument. Bound the await itself.
            async with asyncio.timeout(EDIT_TIMEOUT):
                await self.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard,
                                             disable_web_page_preview=True)
            self._last_text = text
        except TelegramRetryAfter as exc:
            self._retry_at = self.clock() + exc.retry_after
        except TelegramForbiddenError:
            self._unavailable = True
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                self._last_text = text
            else:
                self._unavailable = True
        except Exception as exc:
            self._retry_at = self.clock() + self.refresh_seconds
            log.warning("progress_edit_failed error_type=%s", type(exc).__name__)

    async def _deliver_terminal(self, text, keyboard):
        try:
            async with asyncio.timeout(120):
                for _ in range(5):
                    if self._unavailable or self._last_text == text:
                        return
                    delay = self._retry_at - self.clock()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await self._edit(text, keyboard)
        except TimeoutError:
            log.warning("terminal_progress_delivery_expired")

    async def _run(self):
        next_edit = self.clock() + self.min_edit_seconds
        while not self._closed:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.refresh_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
            delay = max(next_edit, self._retry_at) - self.clock()
            if delay > 0:
                # A terminal result wakes this wait immediately.
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
                if not self._closed and self.clock() < max(next_edit, self._retry_at):
                    continue
            if self._closed or self._unavailable:
                return
            text = progress_text(self.operation, self.story_id, self.event,
                                 self.clock() - self.started, self.clock() - self.stage_started)
            await self._edit(text, self.keyboard())
            next_edit = self.clock() + self.min_edit_seconds

    async def finish(self, text, keyboard=None):
        if self._closed:
            return
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
        await self.finish(outcome_text(outcome, self.clock() - self.started), self.keyboard())
