"""Russian Telegram UI. Business rules and ownership live in the service layer."""

from __future__ import annotations

import html
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.domain import UserError
from app.monitoring import IntensiveSlotOccupied
from app.telegram_progress import ProgressEditBudget, TelegramProgress
from app.telegram_input import AlbumMiddleware, extract_story_input

logger = logging.getLogger(__name__)
MAX_MESSAGE_UNITS = 3900
DENIED = "Это закрытый тест. Попросите организатора прислать ссылку-приглашение."
UNEXPECTED = "Не получилось завершить действие. Попробуйте чуть позже. Ваши наблюдения сохранены."
INTENSIVE_HELP = (
    "⚡ «Следить внимательнее»: проверки через 30 мин, 1, 2, 4, 8, 12 и 24 ч от включения. "
    "Затем — обычное расписание. В тесте — <b>одна такая тема на пользователя</b>."
)


def _units(text: str) -> int:
    return len(text.encode("utf-16-le", errors="replace")) // 2


def escaped(value: Any, limit: int = 700) -> str:
    """Bound escaped HTML in UTF-16 units without splitting an entity."""
    text = str(value or "")
    parts: list[str] = []
    used = 0
    for char in text:
        if ord(char) < 32 and char not in "\n\t":
            continue
        part = html.escape(char, quote=True)
        size = _units(part)
        if used + size > max(0, limit - 1):
            return "".join(parts) + "…"
        parts.append(part)
        used += size
    return "".join(parts)


def safe_url(value: Any) -> str | None:
    """Accept complete web URLs only; never truncate an href."""
    if not isinstance(value, str) or not value or len(value) > 1500:
        return None
    if any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
        if any(char in parsed.hostname for char in '<>"\\'):
            return None
    except (ValueError, UnicodeError):
        return None
    return value


def _sources(update: Any) -> list[str]:
    result: list[str] = []
    for value in (getattr(update, "source_urls", None) or [])[:6]:
        url = safe_url(value)
        if url and url not in result:
            result.append(url)
    return result


def _source_lines(update: Any) -> str:
    lines = []
    for url in _sources(update):
        href = html.escape(url, quote=True)
        if _units(href) > 550:
            continue
        label = escaped(urlsplit(url).hostname or "Источник", 65)
        lines.append(f'• <a href="{href}">{label}</a>')
        if len(lines) == 3:
            break
    if not lines:
        return "Источники: ссылки недоступны." if not _sources(update) else "Источник — по кнопке ниже."
    return "<b>Источники</b>\n" + "\n".join(lines)


def _bullets(items: Any, count: int = 3, limit: int = 220) -> str:
    return "\n".join(f"• {escaped(item, limit)}" for item in (items or [])[:count])


def _date(value: datetime | None) -> str:
    if value is None:
        return "ещё не было"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M МСК")


def _button(text: str, action: str, object_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{action}:{object_id}")


def _keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def notification_text(story: Any, update: Any) -> str:
    demo = bool(getattr(update, "is_demo", False))
    context_only = getattr(update, "update_kind", "development") == "context"
    heading = "🧪 Демонстрационное обновление" if demo else (
        "📌 Уточнение исходной новости" if context_only else "🔔 Есть развитие")
    parts = [heading, f"<b>{escaped(story.title, 180)}</b>"]
    if demo:
        parts.append("Это тест уведомления, не реальная новость. Состояние наблюдения не изменено.")
    elif context_only:
        parts.append("Нашёл важные сведения, которых не было в исходной карточке. Их появление после подписки не подтверждено.")
    label = "Что уточнилось" if context_only and not demo else "Что нового"
    parts.append(f"<b>{label}</b>\n" + escaped(update.summary, 650))
    facts = _bullets(getattr(update, "new_facts", []), 3, 170)
    if facts:
        parts.append(facts)
    if getattr(update, "reason", None):
        parts.append("<b>Почему это важно</b>\n" + escaped(update.reason, 300))
    parts.append(_source_lines(update))
    return "\n\n".join(parts)


def notification_keyboard(story: Any, update: Any) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    sources = _sources(update)
    if sources:
        rows.append([InlineKeyboardButton(text="↗ Открыть источник", url=sources[0])])
    rows.extend([
        [_button("👍 Полезно", "useful", update.id), _button("👎 Неважно", "not_useful", update.id)],
        [_button("⭐ Интересна тема", "interest", story.id)],
        [_button("⏸ Пауза", "pause", story.id), _button("🕒 История", "history", story.id)],
        [_button("💬 Обсудить", "chat", story.id)],
    ])
    return _keyboard(*rows)


def preview_text(story: Any) -> str:
    return (
        f"📰 <b>{escaped(story.title, 180)}</b>\n\n"
        f"<b>Что произошло</b>\n{escaped(story.summary, 850)}\n\n"
        f"<b>Буду отслеживать</b>\n{_bullets(story.watch_goals, 5, 250)}\n\n"
        "Проверьте, верно ли я понял сюжет. Выберите действие:\n\n"
        f"Обычный — первая проверка после подписки, затем каждые {int(story.check_frequency_hours)} ч.\n\n"
        + INTENSIVE_HELP + "\n\n⭐ «Просто интересна тема» — сохранить интерес без наблюдения."
    )


def preview_keyboard(story: Any) -> InlineKeyboardMarkup:
    rows = [[_button("✅ Следить", "watch", story.id)],
            [_button("⚡ Следить внимательнее", "focus", story.id)],
            [_button("⭐ Просто интересна тема", "interest", story.id)],
            [_button("❌ Отмена", "cancel", story.id)]]
    source = safe_url(getattr(story, 'original_url', None))
    if source:
        rows.append([InlineKeyboardButton(text="↗ Исходная новость", url=source)])
    return _keyboard(*rows)


def story_text(story: Any) -> str:
    status = {"active": "🟢 Наблюдаю", "paused": "⏸ На паузе"}.get(story.status, "Черновик")
    intensive = getattr(story, "monitoring_mode", "daily") == "intensive"
    schedule = (INTENSIVE_HELP + f"\nРежим до: {_date(story.intensive_until)}"
                if intensive else f"Проверка каждые {int(story.check_frequency_hours)} ч.\n"
                "В тесте можно выбрать одну тему для режима «Следить внимательнее».")
    if story.status == "paused":
        schedule += "\nПроверки приостановлены."
        if intensive:
            schedule += " Место занято этой темой; срок режима продолжает идти."
    else:
        next_at = getattr(story, "next_check_at", None)
        next_label = _date(next_at) if next_at else "планируется"
        if next_at and next_at <= datetime.now(timezone.utc):
            next_label = "в очереди, начнётся при первой возможности"
        schedule += f"\nБлижайшая проверка: {next_label}"
    return (
        f"📰 <b>{escaped(story.title, 180)}</b>\n{status} · №{story.id}\n\n"
        f"<b>Что известно сейчас</b>\n{escaped(story.current_state, 1050)}\n\n"
        f"<b>Что отслеживаю</b>\n{_bullets(story.watch_goals, 4, 220)}\n\n"
        f"{schedule}\n"
        f"Последняя проверка: {_date(getattr(story, 'last_checked_at', None))}\n"
        f"Последнее развитие: {_date(getattr(story, 'last_meaningful_update_at', None))}"
    )


def story_keyboard(story: Any) -> InlineKeyboardMarkup:
    rows = []
    if story.status == "active":
        rows.append([_button("🔎 Проверить сейчас", "check", story.id), _button("⏸ Пауза", "pause", story.id)])
    else:
        rows.append([_button("▶️ Возобновить", "resume", story.id)])
    if getattr(story, "monitoring_mode", "daily") == "intensive":
        rows.append([_button("🕒 Вернуть обычный режим", "daily", story.id)])
    elif story.status == "active":
        rows.append([_button("⚡ Следить внимательнее", "focus", story.id)])
    rows.extend([
        [_button("⭐ Интересна тема", "interest", story.id)],
        [_button("🕒 История", "history", story.id), _button("💬 Обсудить", "chat", story.id)],
        [_button("🗑 Удалить", "delete", story.id), _button("📋 Все наблюдения", "list", 0)],
    ])
    source = safe_url(getattr(story, 'original_url', None))
    if source:
        rows.append([InlineKeyboardButton(text="↗ Исходная новость", url=source)])
    return _keyboard(*rows)


def interest_text(interest: Any) -> str:
    keywords = ", ".join(escaped(value, 70) for value in (interest.keywords or [])[:6])
    return (
        f"⭐ <b>{escaped(interest.title, 180)}</b>\n\n"
        f"{escaped(interest.summary, 700)}\n\n"
        + (f"О чём: {keywords}\n\n" if keywords else "")
        + "Тема сохранена в ваших интересах для будущих подборок. Подборки пока не запущены. "
        "Эта отметка сама не включает наблюдение.\n\n"
        f"Сохранено: {_date(interest.created_at)}"
    )


def interest_keyboard(interest: Any) -> InlineKeyboardMarkup:
    rows = [[_button("Убрать из интересов", "interest_remove", interest.id)],
            [_button("⭐ Мои интересы", "interests", 0)]]
    source = safe_url(getattr(interest, "source_url", None))
    if source:
        rows.append([InlineKeyboardButton(text="↗ Исходная новость", url=source)])
    return _keyboard(*rows)


def parse_callback(value: str | None) -> tuple[str, int] | None:
    match = re.fullmatch(
        r"(watch|cancel|story|card|focus|daily|check|pause|resume|delete|delete_yes|history|chat|useful|not_useful|list|interest|interests|interest_view|interest_remove):([0-9]{1,10})",
        value or "",
    )
    if not match:
        return None
    action, raw_id = match.groups()
    object_id = int(raw_id)
    if object_id > 2_147_483_647 or (object_id == 0 and action not in {"list", "interests"}):
        return None
    return action, object_id


def parse_transfer(value: str | None) -> tuple[int, int] | None:
    match = re.fullmatch(r"transfer:([1-9][0-9]{0,9}):([1-9][0-9]{0,9})", value or "")
    if not match:
        return None
    target, previous = map(int, match.groups())
    return (target, previous) if max(target, previous) <= 2_147_483_647 and target != previous else None


def _start_argument(message: Message) -> str | None:
    match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?(?:\s+([^\s]+))?\s*", message.text or "")
    return (match.group(1) or "") if match else None


class AccessMiddleware(BaseMiddleware):
    """One security boundary for all messages and callback queries."""

    def __init__(self, service: Any, clock: Any = time.monotonic) -> None:
        self.service = service
        self.clock = clock
        self.last_seen: dict[tuple[int, str], float] = {}

    async def _tell(self, event: Message | CallbackQuery, text: str, alert: bool = False) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text[:190], show_alert=alert)
        else:
            await event.answer(escaped(text, 1800), parse_mode="HTML")

    async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
        if not isinstance(event, (Message, CallbackQuery)):
            return None
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Откройте личный чат с ботом.", show_alert=True)
            return None
        user = event.from_user
        if user is None or user.is_bot:
            return None
        start_arg = _start_argument(event) if isinstance(event, Message) and event.forward_origin is None else None
        try:
            admitted = await self.service.authorize(
                user.id, username=user.username, first_name=user.first_name, start_arg=start_arg or "",
            )
            if admitted is None:
                await self._tell(event, f"{DENIED}\nВаш Telegram ID: {user.id}", alert=True)
                return None
            # /start has its own bucket: the first topic is often sent immediately.
            now = self.clock()
            kind = "start" if start_arg is not None else ("callback" if isinstance(event, CallbackQuery) else "message")
            key = (user.id, kind)
            if now - self.last_seen.get(key, float("-inf")) < 0.7:
                await self._tell(event, "Слишком быстро. Подождите секунду и повторите действие.")
                return None
            if len(self.last_seen) > 1000:
                self.last_seen = {k: v for k, v in self.last_seen.items() if now - v < 60}
            self.last_seen[key] = now
            data["admitted_user"] = admitted
            return await handler(event, data)
        except UserError as exc:
            # UserError is the service's explicit safe-message contract.
            if isinstance(event, CallbackQuery):
                await message.answer(escaped(str(exc), 1800), parse_mode="HTML")
            else:
                await self._tell(event, str(exc))
            return None
        except Exception as exc:
            # Do not log raw exceptions: SDK/transport errors can contain API credentials.
            logger.error("telegram_handler_failed user_id=%s error_type=%s", user.id, type(exc).__name__)
            try:
                await message.answer(UNEXPECTED)
            except Exception:
                logger.warning("telegram_error_reply_failed user_id=%s", user.id)
            return None


async def _answer(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None) -> Message:
    return await message.answer(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)


async def _replace(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise


def _command_id(command: CommandObject) -> int | None:
    if not command.args:
        return None
    value = command.args.strip()
    if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2_147_483_647:
        raise UserError("Укажите номер наблюдения, например /check_now 3. Номера доступны в /watching.")
    return int(value)


def build_router(service: Any, settings: Any) -> Router:
    progress_budget = ProgressEditBudget()
    router = Router(name="news_watch")
    access = AccessMiddleware(service)
    router.message.outer_middleware(AlbumMiddleware())
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)

    async def run_manual_check(message, user_id, story_id):
        progress = await TelegramProgress.begin(message, "check", story_id, budget=progress_budget)
        try:
            await service.request_check(user_id, story_id, progress=progress.update, on_complete=progress.complete)
        except UserError as exc:
            await progress.finish("⚠️ " + escaped(str(exc), 1800), progress.keyboard(), status="error")
        except asyncio.CancelledError:
            await progress.finish("⏹ Запуск проверки прерван. Откройте тему и попробуйте снова.",
                                  progress.keyboard(), status="cancelled")
            raise
        except Exception as exc:
            logger.error("manual_start_failed error_type=%s", type(exc).__name__)
            await progress.finish(UNEXPECTED, progress.keyboard(), status="error")

    async def show_list(message: Message, user_id: int, action: str = "story") -> None:
        stories = await service.list_stories(user_id)
        if not stories:
            await _answer(message, "Наблюдений пока нет. Пришлите ссылку на новость или напишите, за каким сюжетом следить.",
                          _keyboard([_button("⭐ Мои интересы", "interests", 0)]))
            return
        heading = "Какое наблюдение проверить?" if action == "check" else "📋 <b>Мои наблюдения</b>"
        lines = [heading]
        rows = []
        for story in stories[:30]:
            icon = "⏸" if story.status == "paused" else "🟢"
            if getattr(story, "monitoring_mode", "daily") == "intensive":
                icon += "⚡"
            lines.append(f"{icon} №{story.id} · {escaped(story.title, 75)}")
            title = str(story.title or "Наблюдение").replace("\n", " ")[:38]
            rows.append([_button(f"{icon} {story.id}. {title}", action, story.id)])
        lines.append("\nНажмите на наблюдение. Чтобы добавить новое, просто пришлите ссылку или текст.")
        focused = next((item for item in stories if getattr(item, "monitoring_mode", "daily") == "intensive"), None)
        lines.append("\n⚡ В тесте — одна тема с частыми проверками. " + (
            f"Сейчас: №{focused.id}. Режим можно перенести в карточке другой темы." if focused else "Сейчас место свободно."))
        rows.append([_button("⭐ Мои интересы", "interests", 0)])
        await _answer(message, "\n".join(lines), _keyboard(*rows))

    async def show_interests(message: Message, user_id: int, before_id=0, replace=False):
        items = await service.list_interests(user_id, before_id=before_id)
        rows = [[_button(str(item.title).replace("\n", " ")[:48], "interest_view", item.id)] for item in items[:8]]
        if len(items) > 8:
            rows.append([_button("Дальше →", "interests", items[7].id)])
        if before_id:
            rows.append([_button("К началу", "interests", 0)])
        rows.append([_button("📋 Мои наблюдения", "list", 0)])
        text = "⭐ <b>Мои интересы</b>\n\n"
        if items:
            text += "Выберите тему, чтобы посмотреть или убрать её. Эти отметки пригодятся для будущих подборок; сейчас подборки не рассылаются."
        else:
            text += ("Больше тем нет." if before_id else
                     "Пока пусто. Пришлите новость или текст и выберите «⭐ Просто интересна тема». Можно также отметить интерес в существующем наблюдении.")
        await (_replace if replace else _answer)(message, text, _keyboard(*rows))

    @router.message(CommandStart(), ~F.forward_origin)
    async def start(message: Message) -> None:
        ready = "" if service.provider_ready() else "\n\n⚙️ Анализ временно недоступен: администратору нужно настроить API-ключ LLM."
        await _answer(message,
            "📰 <b>Следите за развитием истории</b>\n\n"
            "Пришлите ссылку, перешлите пост из канала или напишите, за чем следить. Посты с фото/видео принимаю по тексту подписи. Я покажу, что понял, и попрошу подтвердить наблюдение.\n\n"
            f"Проверяю каждые {int(settings.default_check_interval_hours)} ч. Уведомляю, когда появляются существенные новые сведения. Пересказы стараюсь пропускать.\n\n"
            + INTENSIVE_HELP + "\n\n"
            "Например: «Когда откроют новую станцию метро и изменились ли сроки?»\n\n"
            "Можно выбрать «⭐ Просто интересна тема» — запомню интерес для будущих подборок без запуска наблюдения.\n\n"
            "Ваши сюжеты — /watching · Мои интересы — /interests · Помощь — /help" + ready,
            _keyboard([_button("📋 Мои наблюдения", "list", 0), _button("⭐ Мои интересы", "interests", 0)]))

    @router.message(Command("help"), ~F.forward_origin)
    async def help_command(message: Message) -> None:
        await _answer(message,
            "<b>Как пользоваться</b>\n\n"
            "1. Пришлите ссылку, текст новости или перешлите пост из канала — с текстом либо подписью к фото/видео. Альбом принимаю как одну новость.\n"
            "2. Проверьте карточку и нажмите «Следить» либо «⭐ Просто интересна тема», чтобы только сохранить интерес.\n"
            "3. Получайте уведомления о развитии истории и отмечайте, были ли они полезны.\n\n"
            f"Автоматическая проверка — каждые {int(settings.default_check_interval_hours)} ч. "
            f"До {int(settings.max_stories_per_user)} наблюдений на человека. "
            f"Ручных проверок — до {int(settings.max_manual_checks_per_day)} в сутки; "
            f"между ними минимум {int(settings.manual_check_cooldown_seconds)} сек.\n\n"
            + INTENSIVE_HELP + "\n"
            "Время считается от включения режима, а не от предыдущей проверки. На паузе срок продолжает идти, "
            "и тема занимает место до выключения, переноса или окончания режима. "
            "Ручная проверка не откладывает автоматическую. Уведомления приходят только при новых важных фактах.\n\n"
            "/watching — список, история, пауза и удаление\n"
            "/interests — ваши интересы для будущих подборок; просмотр и удаление\n"
            "/check_now — проверить выбранное наблюдение\n"
            "/cancel — как отменить создание\n\n"
            "Поиск может пропускать публикации, а ИИ — ошибаться. Сверяйте важные выводы с источниками. "
            "Не отправляйте пароли и личные документы. Удаление наблюдения удаляет его сохранённые тексты и историю. "
            "Отдельно отмеченный интерес сохраняется; убрать его можно в /interests. Подборки пока не запущены.")

    @router.message(Command("interests"), ~F.forward_origin)
    async def interests_command(message: Message) -> None:
        await show_interests(message, message.from_user.id)

    @router.message(Command("watching"), ~F.forward_origin)
    async def watching(message: Message) -> None:
        await show_list(message, message.from_user.id)

    @router.message(Command("cancel"), ~F.forward_origin)
    async def cancel(message: Message) -> None:
        await _answer(message, "Чтобы отменить создание, нажмите «Отмена» под карточкой предпросмотра. Неподтверждённый сюжет не отслеживается. Можно сразу прислать другую тему.")

    @router.message(Command("check_now"), ~F.forward_origin)
    async def check_now(message: Message, command: CommandObject) -> None:
        story_id = _command_id(command)
        if story_id is None:
            await show_list(message, message.from_user.id, "check")
        else:
            await run_manual_check(message, message.from_user.id, story_id)

    @router.message(Command("admin"), ~F.forward_origin)
    async def admin(message: Message) -> None:
        user_id = message.from_user.id
        if not await service.is_admin(user_id):
            raise UserError("Эта команда доступна только администратору.")
        summary = await service.admin_summary(user_id)
        text = "📊 <b>Закрытый тест</b>\n\n" + escaped(summary, 2600)
        # Only the tester invite appears here; the admin claim token never enters UI.
        if settings.invite_code and re.fullmatch(r"[A-Za-z0-9_-]{1,57}", settings.invite_code):
            me = await message.bot.get_me()
            if me.username and re.fullmatch(r"[A-Za-z0-9_]+", me.username):
                url = f"https://t.me/{me.username}?start=invite_{settings.invite_code}"
                text += f'\n\n<a href="{html.escape(url, quote=True)}">Приглашение для тестировщика</a>\nПередавайте только участникам закрытого теста.'
        text += "\n\n/admin_errors — последние ошибки\n/demo_update — тестовое уведомление (если включён demo mode)"
        await _answer(message, text)

    @router.message(Command("admin_errors"), ~F.forward_origin)
    async def admin_errors(message: Message) -> None:
        if not await service.is_admin(message.from_user.id):
            raise UserError("Эта команда доступна только администратору.")
        errors = await service.admin_errors(message.from_user.id)
        await _answer(message, "🛠 <b>Последние ошибки</b>\n\n" + escaped(errors, 3300))

    @router.message(Command("demo_update"), ~F.forward_origin)
    async def demo_update(message: Message, command: CommandObject) -> None:
        if not await service.is_admin(message.from_user.id):
            raise UserError("Эта команда доступна только администратору.")
        result = await service.demo_update(message.from_user.id, _command_id(command))
        await _answer(message, escaped(result, 1800))

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        transfer = parse_transfer(query.data)
        parsed = parse_callback(query.data)
        if parsed is None and transfer is None:
            await query.answer("Эта кнопка устарела. Откройте /watching.", show_alert=True)
            return
        action, object_id = parsed if parsed else ("focus", transfer[0])
        user_id = query.from_user.id
        message = query.message
        if action == "interests":
            await query.answer()
            await show_interests(message, user_id, before_id=object_id, replace=True)
            return
        if action == "interest_view":
            await query.answer()
            interest = await service.get_interest(user_id, object_id)
            if interest is None:
                raise UserError("Интерес уже удалён или недоступен. Откройте /interests.")
            await _replace(message, interest_text(interest), interest_keyboard(interest))
            return
        if action == "interest_remove":
            removed = await service.remove_interest(user_id, object_id)
            if not removed:
                await query.answer("Уже удалён или недоступен.", show_alert=True)
                return
            await query.answer("Интерес убран.")
            await _replace(message, "Отметка интереса убрана. Ваши наблюдения не изменились.",
                           _keyboard([_button("⭐ Мои интересы", "interests", 0)]))
            return
        if action == "interest":
            result = await service.save_interest(user_id, object_id)
            if not result.created and result.monitoring_status in {"active", "paused"}:
                await query.answer("Уже в ваших интересах. Посмотреть или убрать: /interests.", show_alert=True)
                return
            await query.answer("Интерес сохранён." if result.created else "Уже в ваших интересах.")
            prefix = {"active": "Наблюдение продолжает работать.\n\n",
                      "paused": "Наблюдение остаётся на паузе.\n\n"}.get(result.monitoring_status, "Наблюдение не включено.\n\n")
            send = _answer if result.monitoring_status in {"active", "paused"} else _replace
            await send(message, prefix + interest_text(result.interest), interest_keyboard(result.interest))
            return
        if action == "focus":
            await query.answer()
            try:
                changed = await service.set_monitoring_mode(user_id, object_id, "intensive",
                    replace_story_id=transfer[1] if transfer else None)
            except IntensiveSlotOccupied as exc:
                await _answer(message,
                    "⚡ В тесте — <b>одна тема с частыми проверками</b>.\n\n"
                    f"Сейчас это «{escaped(exc.title, 180)}». Перенести режим на выбранную тему? "
                    "Прежняя тема вернётся к обычному расписанию; если она на паузе, пауза сохранится.\n\n" + INTENSIVE_HELP,
                    _keyboard([InlineKeyboardButton(text="⚡ Перенести режим", callback_data=f"transfer:{object_id}:{exc.story_id}")],
                              [_button("Оставить как есть", "card", object_id)]))
                return
            await _replace(message, "✅ Режим «Следить внимательнее» включён.\n\n" + story_text(changed), story_keyboard(changed))
            return
        if action == "card":
            await query.answer()
            current = await service.get_story(user_id, object_id)
            if current is None:
                raise UserError("Наблюдение больше недоступно. Откройте /watching.")
            await _replace(message, preview_text(current) if current.status == "draft" else story_text(current),
                           preview_keyboard(current) if current.status == "draft" else story_keyboard(current))
            return
        if action in {"useful", "not_useful"}:
            saved = await service.give_feedback(user_id, object_id, action)
            await query.answer("Спасибо! Оценка сохранена." if saved else "Обновление больше недоступно.", show_alert=not saved)
            return
        if action == "list":
            await query.answer()
            await show_list(message, user_id)
            return
        if action == "watch":
            await query.answer()
            story = await service.confirm_story(user_id, object_id)
            await _replace(message, "✅ Наблюдение создано.\n\n" + story_text(story), story_keyboard(story))
            return
        if action == "cancel":
            await query.answer()
            await service.cancel_draft(user_id, object_id)
            await _replace(message, "Создание отменено. Пришлите другую ссылку или тему, когда захотите.")
            return
        story = await service.get_story(user_id, object_id)
        if story is None or story.status not in {"active", "paused"}:
            await query.answer("Наблюдение больше недоступно. Откройте /watching.", show_alert=True)
            return
        if action == "chat":
            await query.answer("Обсуждение сюжета появится в следующей версии. Пока доступны наблюдение, источники и история.", show_alert=True)
            return
        await query.answer()
        if action == "story":
            await _answer(message, story_text(story), story_keyboard(story))
        elif action == "daily":
            changed = await service.set_monitoring_mode(user_id, object_id, "daily")
            await _replace(message, "✅ В этой теме обычный режим.\n\n" + story_text(changed), story_keyboard(changed))
        elif action == "check":
            await run_manual_check(message, user_id, object_id)
        elif action in {"pause", "resume"}:
            changed = await service.set_status(user_id, object_id, "paused" if action == "pause" else "active")
            if changed is None:
                raise UserError("Наблюдение больше недоступно. Откройте /watching.")
            await _answer(message, story_text(changed), story_keyboard(changed))
        elif action == "delete":
            await _answer(message,
                f"Удалить наблюдение «{escaped(story.title, 200)}»?\n\nЕго тексты, источники и история будут удалены. Это действие нельзя отменить. "
                "Отдельно отмеченный интерес останется в /interests; там его можно убрать.",
                _keyboard([_button("🗑 Да, удалить", "delete_yes", object_id), _button("Оставить", "story", object_id)]))
        elif action == "delete_yes":
            await service.set_status(user_id, object_id, "deleted")
            await _replace(message, "Наблюдение удалено. Ваши остальные сюжеты — /watching.")
        elif action == "history":
            updates = await service.recent_updates(user_id, object_id)
            if not updates:
                await _answer(message, "Существенных обновлений пока не было. Первое появится здесь, когда история получит развитие.", story_keyboard(story))
            else:
                await _answer(message, f"🕒 <b>Последние обновления</b>\n{escaped(story.title, 180)}")
                for update in updates[:5]:
                    await _answer(message, _date(update.created_at) + "\n\n" + notification_text(story, update), notification_keyboard(story, update))

    @router.message(F.text.startswith("/"), ~F.forward_origin)
    async def unknown_command(message: Message) -> None:
        await _answer(message, "Не знаю эту команду. /help — подсказка, /watching — ваши наблюдения. Для нового сюжета просто пришлите ссылку или текст.")

    @router.message(F.text | F.caption)
    async def new_story(message: Message, album_messages: list[Message] | None = None) -> None:
        seed = extract_story_input(message, album_messages)
        text = seed.text
        if len(text) < 10:
            raise UserError("Добавьте немного подробностей: что произошло и какое развитие вас интересует?")
        if len(text) > 10000:
            raise UserError("Текст слишком длинный. Пришлите ссылку или описание до 10 000 символов.")
        if not service.provider_ready():
            raise UserError("Анализ пока недоступен: администратору нужно настроить API-ключ LLM. Попробуйте позже.")
        progress = await TelegramProgress.begin(message, "prepare", budget=progress_budget)
        try:
            options = {'source_url': seed.source_url, 'use_text': True} if seed.use_text else {}
            story = await service.prepare_story(message.from_user.id, text, progress=progress.update, **options)
        except UserError as exc:
            await progress.finish("⚠️ " + escaped(str(exc), 1800), status="error")
        except asyncio.CancelledError:
            await progress.finish("⏹ Подготовка прервана. Пришлите новость ещё раз.", status="cancelled")
            raise
        except Exception as exc:
            logger.error("story_prepare_failed error_type=%s", type(exc).__name__)
            await progress.finish(UNEXPECTED, status="error")
        else:
            await progress.finish(preview_text(story), preview_keyboard(story))

    @router.message()
    async def unsupported(message: Message) -> None:
        await _answer(message, "В этом сообщении нет текста новости. Перешлите пост с текстом или подписью к фото/видео либо кратко опишите событие. Содержимое самих фото, видео и голосовых пока не распознаю.")

    return router
