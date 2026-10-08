"""Russian Telegram UI. Business rules and ownership live in the service layer."""

from __future__ import annotations

import html
import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from urllib.parse import urlsplit

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, ChatMemberUpdated, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from app.domain import UserError
from app.monitoring import IntensiveSlotOccupied
from app.navigation import main_keyboard, safe_url, with_home
from app import user_news
from app.journal import DATE_HELP, DATE_PROMPT, PAGE_SIZE, date_window, parse_journal_callback, preset_window
from app.telegram_progress import ProgressEditBudget, TelegramProgress
from app.telegram_input import AlbumMiddleware, extract_story_input
from app.product_analytics import interaction_category
from app.account_ui import account_keyboard
from app.daily_reports import REPORT_PROMPT, report_keyboard

logger = logging.getLogger(__name__)
MAX_MESSAGE_UNITS = 3900
DENIED = "Это закрытый тест. Попросите организатора прислать ссылку-приглашение."
UNEXPECTED = "Не получилось завершить действие. Попробуйте чуть позже. Ваши наблюдения сохранены."
INTENSIVE_HELP = (
    "⚡ «Следить внимательнее»: проверки через 30 мин, 1, 2, 4, 8, 12 и 24 ч от включения. "
    "Важные изменения присылаю сразу после проверки. Затем — ежедневный отчёт. "
    "Количество срочных тем зависит от вашего тарифа: /account."
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
    return with_home(rows)


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
    rows: list[list[InlineKeyboardButton]] = [[_button("📖 Читать дальше", "full", update.id)]]
    sources = _sources(update)
    if sources:
        rows.append([InlineKeyboardButton(text="↗ Открыть источник", url=sources[0])])
    rows.extend([
        [_button("👍 Полезно", "useful", update.id), _button("👎 Неважно", "not_useful", update.id)],
        [_button("⭐ Интересна тема", "interest", story.id)],
        [_button("⏸ Пауза", "pause", story.id), _button("🕒 История", "history", story.id)],
        [_button("💬 Обсудить", "chat", story.id)],
        [InlineKeyboardButton(text="🗂 Журнал уведомлений", callback_data="jp:week")],
    ])
    return _keyboard(*rows)


def full_update_pages(story, update):
    """Escape chunks independently, preserving every stored character and fact."""
    context = getattr(update, 'update_kind', 'development') == 'context'
    lines = ['📖 Полный отчёт о новости', story.title]
    if getattr(update, 'is_demo', False):
        lines.append('🧪 Демонстрация: это тестовое сообщение, не реальная новость.')
    elif context:
        lines.append('Уточнение исходной новости. Появление сведений после подписки не подтверждено.')
    lines.extend(['Что уточнилось' if context else 'Что нового', update.summary])
    lines.extend('• ' + str(fact) for fact in (getattr(update, 'new_facts', None) or []))
    if getattr(update, 'reason', None):
        lines.extend(['Почему это важно', update.reason])
    if getattr(update, 'new_state', None):
        lines.extend(['Что известно сейчас', update.new_state])
    sources = [url for url in (getattr(update, 'source_urls', None) or []) if safe_url(url)]
    if sources:
        lines.extend(['Источники', *sources])
    text = '\n\n'.join(str(line) for line in lines)
    pages, current, units = [], [], 0
    for char in text:
        if ord(char) < 32 and char not in '\n\t':
            continue
        part = html.escape(char, quote=True)
        size = _units(part)
        if units + size > 3500:
            # Prefer a word boundary near the end; keep whitespace in the output.
            boundary = next((i + 1 for i in range(len(current) - 1, max(-1, len(current) - 400), -1)
                             if current[i] in {' ', '\n', '\t'}), len(current))
            pages.append(''.join(current[:boundary]))
            current = current[boundary:]
            units = sum(_units(value) for value in current)
        current.append(part)
        units += size
    if current:
        pages.append(''.join(current))
    return pages


def preview_text(story: Any) -> str:
    return (
        f"📰 <b>{escaped(story.title, 180)}</b>\n\n"
        f"<b>Что произошло</b>\n{escaped(story.summary, 850)}\n\n"
        f"<b>Буду отслеживать</b>\n{_bullets(story.watch_goals, 5, 250)}\n\n"
        "Проверьте, верно ли я понял сюжет. Выберите действие:\n\n"
        "Ежедневный отчёт — все обычные подписки в одном сообщении в выбранное время. Без изменений — прочерк.\n\n"
        + INTENSIVE_HELP + "\n\n⭐ «Просто интересна тема» — сохранить интерес без наблюдения."
    )


def preview_keyboard(story: Any) -> InlineKeyboardMarkup:
    rows = [[_button("🗓 В ежедневный отчёт", "watch", story.id)],
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
                if intensive else "🗓 В ежедневном отчёте. Время по Москве: /report.\n"
                f"Проверка каждые {int(story.check_frequency_hours)} ч.")
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
        rows.append([_button("🗓 В ежедневный отчёт", "daily", story.id)])
    elif story.status == "active":
        rows.append([_button("⚡ Следить внимательнее", "focus", story.id)])
    rows.extend([
        [InlineKeyboardButton(text="🕒 Время отчёта", callback_data="report:settings")],
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
            await event.answer(escaped(text, 1800), parse_mode="HTML", reply_markup=with_home())

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
            track = getattr(self.service, 'track_interaction', None)
            if track:
                event_key = f'callback:{event.id}' if isinstance(event, CallbackQuery) else f'message:{event.message_id}'
                await track(user.id, interaction_category(event), event_key)
            # /start has its own bucket: the first topic is often sent immediately.
            now = self.clock()
            kind = "start" if start_arg is not None else ("callback" if isinstance(event, CallbackQuery) else "message")
            key = (user.id, kind)
            if now - self.last_seen.get(key, float("-inf")) < 0.7:
                is_date_reply = (isinstance(event, Message) and event.reply_to_message
                    and (event.reply_to_message.text or '').startswith(DATE_PROMPT))
                if (isinstance(event, Message) and (event.text or event.caption)
                        and not is_date_reply and (event.forward_origin or not (event.text or '').startswith('/'))
                        and (event.text or '').strip().casefold() not in {'admin', 'админ'}):
                    seed = extract_story_input(event, data.get('album_messages'))
                    item = await self.service.save_user_news(user.id, seed.text, source_url=seed.source_url,
                        use_text=seed.use_text, input_message_id=event.message_id)
                    await event.answer('Слишком быстро для нового анализа. Новость сохранена в «Новости пользователя». '
                        'Откройте её и нажмите «Повторить обработку», когда закончится предыдущая.',
                        reply_markup=with_home([[InlineKeyboardButton(text='📥 Открыть новость', callback_data=f'nopen:{item.id}')]]))
                    return None
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
                await message.answer(escaped(str(exc), 1800), parse_mode="HTML", reply_markup=with_home())
            else:
                await self._tell(event, str(exc))
            return None
        except Exception as exc:
            # Do not log raw exceptions: SDK/transport errors can contain API credentials.
            logger.error("telegram_handler_failed user_id=%s error_type=%s", user.id, type(exc).__name__)
            try:
                await message.answer(UNEXPECTED, reply_markup=with_home())
            except Exception:
                logger.warning("telegram_error_reply_failed user_id=%s", user.id)
            return None


async def _answer(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None) -> Message:
    return await message.answer(text, parse_mode="HTML", reply_markup=keyboard or with_home(), disable_web_page_preview=True)


async def _replace(message: Message, text: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard or with_home(), disable_web_page_preview=True)
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
    confirmations = {}

    def confirmation(uid, action, value=None):
        now = time.monotonic()
        for owner, pending in list(confirmations.items()):
            if pending[3] < now:
                confirmations.pop(owner, None)
        token = uuid4().hex[:16]
        confirmations[uid] = (token, action, value, now + 300)
        return token

    def consume_confirmation(uid, token, action):
        pending = confirmations.get(uid)
        if not pending or pending[0] != token or pending[1] != action or pending[3] < time.monotonic():
            raise UserError('Подтверждение устарело. Откройте раздел заново.')
        confirmations.pop(uid, None)
        return pending[2]

    async def show_settings(message, uid, replace=True):
        pref = await service.repo.report_preference(uid)
        value = await service.commerce.snapshot(uid)
        enabled = not value['account'] or value['account'].promotions_enabled
        current = f'{pref.minute // 60:02d}:{pref.minute % 60:02d} МСК' if pref and pref.minute is not None else 'не выбрано'
        rows = [[InlineKeyboardButton(text='🕒 Время ежедневного отчёта', callback_data='report:settings')],
            [InlineKeyboardButton(text='🔕 Отключить предложения' if enabled else '🔔 Включить предложения',
                                  callback_data=f'settings:ads:{0 if enabled else 1}')],
            [InlineKeyboardButton(text='💎 Тариф и баланс', callback_data='account:home')],
            [InlineKeyboardButton(text='🗑 Начать заново', callback_data='settings:reset')]]
        await (_replace if replace else _answer)(message,
            f'⚙️ <b>Настройки аккаунта</b>\n\nОтчёт: {current}\n'
            f'Предложения и скидки: {"включены" if enabled else "выключены"}\n\n'
            'Здесь можно изменить расписание, управлять предложениями и очистить свои новости и темы.', _keyboard(*rows))

    async def show_catalog(message, uid, admin=False):
        if admin and not await service.is_admin(uid):
            raise UserError('Это действие доступно только администратору.')
        plans = await service.commerce.catalog()
        rows = [[InlineKeyboardButton(text=f'{p.name[:35]} · {p.price_minor / 100:.2f} {p.currency}',
                 callback_data=f'{"admplan" if admin else "shop"}:{p.id}')] for p in plans]
        if admin:
            rows.append([InlineKeyboardButton(text='🗑 Снять мой тариф', callback_data='admplan:0')])
            rows.append([InlineKeyboardButton(text='← Админ-меню', callback_data='admin:home')])
        else:
            rows.append([InlineKeyboardButton(text='💎 Мой тариф', callback_data='account:home')])
        await _replace(message, ('🛠 <b>Мои тестовые подписки</b>\n\n'
            'Выберите тариф для своего аккаунта. Подключение тестовое, без списания денег. '
            'Снятие тарифа возвращает базовые возможности. Индивидуальные лимиты сохраняются.' if admin else
            '🛍 <b>Тарифы</b>\n\nВыберите тариф, чтобы посмотреть возможности и условия. '
            'Онлайн-оплата пока в разработке.') + ('\n\nТарифов пока нет. Администратор может добавить их в админке.' if not plans else ''),
            _keyboard(*rows))

    def admin_keyboard():
        rows = [[InlineKeyboardButton(text='📊 Статистика', callback_data='admin:stats'),
                          InlineKeyboardButton(text='🛠 Ошибки', callback_data='admin:errors')],
                         [InlineKeyboardButton(text='💎 Мои тестовые подписки', callback_data='admin:plans')],
                         [InlineKeyboardButton(text='🧪 Тест уведомления', callback_data='admin:demo')],
                         [InlineKeyboardButton(text='📣 Рассылки и управление', callback_data='admin:web')]]
        url = getattr(settings, 'admin_panel_url', '')
        try:
            parsed = urlsplit(url)
            if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and not any(c.isspace() for c in url):
                rows.append([InlineKeyboardButton(text='🌐 Открыть админку', url=url)])
        except ValueError:
            pass
        return _keyboard(*rows)

    async def show_admin(message, uid, replace=True):
        if not await service.is_admin(uid):
            raise UserError('Эта команда доступна только администратору.')
        text = ('🛠 <b>Админ-меню</b>\n\nСтатистика, ошибки, тест уведомлений и управление своим тарифом. '
                'Рассылки, цены и роли пользователей настраиваются в защищённой веб-админке.')
        if settings.invite_code and re.fullmatch(r'[A-Za-z0-9_-]{1,57}', settings.invite_code):
            me = await message.bot.get_me()
            if me.username and re.fullmatch(r'[A-Za-z0-9_]+', me.username):
                url = f'https://t.me/{me.username}?start=invite_{settings.invite_code}'
                text += f'\n\n<a href="{html.escape(url, quote=True)}">Приглашение для пользователя</a>'
        await (_replace if replace else _answer)(message, text, admin_keyboard())
    access = AccessMiddleware(service)
    router.message.outer_middleware(AlbumMiddleware())
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)

    @router.my_chat_member()
    async def membership(event: ChatMemberUpdated, event_update=None):
        if event.chat.type != 'private':
            return
        old, new = event.old_chat_member.status, event.new_chat_member.status
        if (old == 'kicked') == (new == 'kicked'):
            return
        key = str(event_update.update_id) if event_update is not None else f'{event.date.isoformat()}:{old}:{new}'
        await service.track_membership(event.chat.id, new == 'kicked', f'membership:{key}')

    async def show_main(message, replace=False):
        await (_replace if replace else _answer)(message,
            '🏠 <b>Главное меню</b>\n\nПришлите новость, ссылку или пост из канала. '
            'Сначала сохраню её, затем обработаю и отдельно сообщу, когда можно выбрать действие.\n\n'
            '📥 <b>Новости пользователя</b> — всё, что вы прислали, включая отложенное и ошибки обработки.\n'
            '🗂 <b>Журнал</b> — отправленные уведомления о развитии новостей.\n\n'
            '🗓 Один ежедневный отчёт по вашим историям. Для срочных тем — «Следить внимательнее». '
            'Расписание и управление данными — в настройках аккаунта.',
            main_keyboard())

    async def show_news(message, user_id, before_id=0, replace=False):
        items = await service.list_user_news(user_id, before_id)
        rows = []
        for item in items[:8]:
            title = (item.parsed_data or {}).get('title') or item.original_text
            rows.append([InlineKeyboardButton(text=f'{user_news.label(item)} · {str(title).replace(chr(10), " ")[:38]}',
                                              callback_data=f'nopen:{item.id}')])
        if len(items) > 8:
            rows.append([InlineKeyboardButton(text='Дальше →', callback_data=f'news:{items[7].id}')])
        if before_id:
            rows.append([InlineKeyboardButton(text='← К началу', callback_data='news:0')])
        text = ('📥 <b>Новости пользователя</b>\n\n' +
                ('Выберите новость. Последние — сверху. Обработанную карточку можно открыть без повторного анализа.'
                 if items else 'Пока пусто. Пришлите новость, текст или ссылку — она появится здесь ещё до обработки.'))
        await (_replace if replace else _answer)(message, text, _keyboard(*rows))

    async def show_news_item(message, user_id, item, replace=True):
        story = await service.get_story(user_id, item.story_id) if item.story_id else None
        if item.status == 'ready':
            text = '📥 <b>Сохранённая новость</b>\n\n' + preview_text(user_news.preview(item, settings.default_check_interval_hours))
            if story and story.status in {'active', 'paused'}:
                text = f'📥 <b>Сохранённая новость</b> · {user_news.label(item)}\n\n' + story_text(story)
        else:
            text = (f'📥 <b>Сохранённая новость</b>\n{user_news.label(item)}\n\n' +
                    escaped(item.original_text, 1400) + '\n\n' + escaped(item.error_message or
                    'Можно вернуться в главное меню. Когда обработка завершится, пришлю отдельное сообщение.', 1000))
        await (_replace if replace else _answer)(message, text, user_news.keyboard(item, story))

    async def process_news(message, user_id, item):
        progress = await TelegramProgress.begin(message, 'prepare', budget=progress_budget)
        try:
            ready = await service.process_user_news(user_id, item.id, progress.update)
        except UserError as exc:
            if await service.get_user_news(user_id, item.id) is None:
                await progress.finish('Новость удалена. Обработка остановлена.', status='cancelled')
                return
            await progress.finish('⚠️ ' + escaped(str(exc), 1500) +
                '\n\nНовость сохранена в «Новости пользователя».',
                _keyboard([InlineKeyboardButton(text='📥 Открыть новость', callback_data=f'nopen:{item.id}')]), status='error')
        except asyncio.CancelledError:
            await progress.finish('⏹ Обработка прервана. Новость сохранена — её можно повторить из списка.',
                _keyboard([InlineKeyboardButton(text='📥 Открыть новость', callback_data=f'nopen:{item.id}')]), status='cancelled')
            raise
        except Exception as exc:
            logger.error('news_processing_failed error_type=%s', type(exc).__name__)
            await progress.finish(UNEXPECTED + '\n\nНовость сохранена в «Новости пользователя».',
                _keyboard([InlineKeyboardButton(text='📥 Открыть новость', callback_data=f'nopen:{item.id}')]), status='error')
        else:
            await progress.finish(preview_text(user_news.preview(ready, settings.default_check_interval_hours)),
                                  user_news.keyboard(ready))

    @router.message(Command('menu'), ~F.forward_origin)
    async def menu_command(message: Message):
        await show_main(message)

    @router.message(Command('news'), ~F.forward_origin)
    async def news_command(message: Message):
        await show_news(message, message.from_user.id)

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
                          _keyboard([_button("⭐ Мои интересы", "interests", 0)],
                                    [InlineKeyboardButton(text="🗂 Журнал уведомлений", callback_data="jp:week")]))
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
        lines.append("\n⚡ Лимит срочных тем — в /account. " + (
            f"Сейчас: №{focused.id}. Режим можно перенести в карточке другой темы." if focused else "Сейчас место свободно."))
        rows.append([_button("⭐ Мои интересы", "interests", 0)])
        rows.append([InlineKeyboardButton(text="🗂 Журнал уведомлений", callback_data="jp:week")])
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

    async def ask_journal_dates(message, error=None):
        text = DATE_PROMPT + '\n\n' + (str(error) if error else DATE_HELP)
        await message.answer(escaped(text, 1800), parse_mode='HTML',
                             reply_markup=ForceReply(input_field_placeholder='23.09.2026 30.09.2026', selective=True))
        await _answer(message, 'Можно выбрать период ответом выше или вернуться в главное меню.')

    async def show_journal(message, user_id, window=None, cursor=0, replace=False):
        window = window or preset_window()
        items = await service.list_notifications(user_id, window, before_id=cursor)
        rows = []
        for update, story in items[:PAGE_SIZE]:
            icon = '🧪' if update.is_demo else ('📌' if update.update_kind == 'context' else '🔔')
            title = str(story.title).replace('\n', ' ')[:38]
            label = f'{icon} {_date(update.notified_at)[0:5]} {_date(update.notified_at)[11:16]} · {title}'
            rows.append([InlineKeyboardButton(text=label, callback_data=window.callback('ju', cursor, update.id))])
        navigation = []
        if cursor:
            navigation.append(InlineKeyboardButton(text='← К началу периода', callback_data=window.callback()))
        if len(items) > PAGE_SIZE:
            navigation.append(InlineKeyboardButton(text='Дальше →', callback_data=window.callback(cursor=items[PAGE_SIZE-1][0].id)))
        if navigation:
            rows.append(navigation)
        for choices in [(('Сегодня', 'today'), ('24 часа', 'day'), ('7 дней', 'week')),
                        (('30 дней', 'month'), ('Всё время', 'all'), ('📅 Даты', 'custom'))]:
            rows.append([InlineKeyboardButton(text=label, callback_data=f'jp:{key}') for label, key in choices])
        rows.append([_button('📋 Мои наблюдения', 'list', 0)])
        body = ('Нажмите на уведомление. Последние — сверху.' if items else
                'За этот период уведомлений нет. Выберите другой период или вернитесь позже.')
        text = ('🗂 <b>Журнал уведомлений</b>\n' + escaped(window.label, 160) + '\n\n' + body +
                '\n\n🔔 Развитие · 📌 Уточнение · 🧪 Демо\n'
                'Период считается по времени отправки. История удаляется вместе с наблюдением.')
        await (_replace if replace else _answer)(message, text, _keyboard(*rows))

    @router.message(Command('journal'), ~F.forward_origin)
    async def journal_command(message: Message, command: CommandObject):
        window = date_window(command.args) if command.args else preset_window()
        await show_journal(message, message.from_user.id, window)

    @router.message(F.text, ~F.text.startswith('/'), ~F.forward_origin,
                    F.reply_to_message.from_user.is_bot, F.reply_to_message.text.startswith(DATE_PROMPT))
    async def journal_dates_reply(message: Message):
        if message.reply_to_message.from_user.id != message.bot.id:
            raise UserError('Откройте выбор периода через /journal.')
        try:
            window = date_window(message.text)
        except UserError as exc:
            await ask_journal_dates(message, exc)
            return
        await show_journal(message, message.from_user.id, window)

    async def journal_callback(query):
        data = query.data or ''
        if data.startswith('jp:'):
            period = data[3:]
            if period not in {'today', 'day', 'week', 'month', 'all', 'custom'}:
                await query.answer('Кнопка устарела. Откройте /journal.', show_alert=True)
                return
            await query.answer()
            if period == 'custom':
                await ask_journal_dates(query.message)
            else:
                await show_journal(query.message, query.from_user.id, preset_window(period), replace=True)
            return
        parsed = parse_journal_callback(data)
        if parsed is None:
            await query.answer('Кнопка устарела. Откройте /journal.', show_alert=True)
            return
        action, window, cursor, update_id = parsed
        if action == 'jn':
            await query.answer()
            await show_journal(query.message, query.from_user.id, window, cursor, replace=True)
            return
        item = await service.get_notification(query.from_user.id, update_id, window)
        if item is None:
            await query.answer('Уведомление удалено или недоступно в этом периоде.', show_alert=True)
            return
        update, story = item
        await query.answer()
        text = f'🗂 Отправлено: {_date(update.notified_at)}\n\n' + notification_text(story, update)
        rows = list(notification_keyboard(story, update).inline_keyboard)
        rows = [[b for b in row if b.callback_data not in {'jp:week', 'menu:0'}] for row in rows]
        rows = [row for row in rows if row]
        rows.append([_button('📋 Открыть тему', 'story', story.id)])
        anchor = getattr(update, 'telegram_message_id', None)
        if action == 'ju' and anchor:
            rows.append([InlineKeyboardButton(text='↩ Показать в чате', callback_data=window.callback('jo', cursor, update.id))])
        rows.append([InlineKeyboardButton(text='← Назад в журнал', callback_data=window.callback(cursor=cursor))])
        keyboard = _keyboard(*rows)
        if action == 'jo':
            # Private bot chats have no public post links. A reply points back
            # to the actual message; if it was deleted, the saved card still opens.
            await query.message.answer(text, parse_mode='HTML', reply_markup=keyboard,
                disable_web_page_preview=True,
                reply_parameters=ReplyParameters(message_id=anchor, allow_sending_without_reply=True) if anchor else None)
        else:
            await _replace(query.message, text, keyboard)

    @router.message(CommandStart(), ~F.forward_origin)
    async def start(message: Message) -> None:
        ready = "" if service.provider_ready() else "\n\n⚙️ Анализ временно недоступен: администратору нужно настроить API-ключ LLM."
        await _answer(message,
            "📰 <b>Ваши новости — в одном ежедневном отчёте</b>\n\n"
            "Пришлите ссылку, перешлите пост или напишите тему. Сохраню новость, обработаю её и предложу выбрать действие.\n\n"
            "🗓 <b>Ежедневный отчёт</b> — коротко о развитии ваших историй в выбранное время по Москве. Без изменений — прочерк.\n"
            "⚡ <b>Следить внимательнее</b> — важные изменения после срочных проверок.\n"
            "⭐ <b>Просто интересна тема</b> — сохранить интерес без наблюдения.\n\n"
            "Расписание и управление данными — в настройках аккаунта. Тарифы — в каталоге. Подробности — в помощи." + ready,
            main_keyboard())

    @router.message(Command("help"), ~F.forward_origin)
    async def help_command(message: Message) -> None:
        await _answer(message,
            "<b>Как пользоваться</b>\n\n"
            "1. Пришлите ссылку, текст новости или перешлите пост из канала — с текстом либо подписью к фото/видео. Альбом принимаю как одну новость.\n"
            "2. Нажмите «В ежедневный отчёт» и выберите время или включите «Следить внимательнее».\n"
            "3. Получайте отчёты. «Читать дальше» открывает полный текст обновления.\n\n"
            f"Автоматическая проверка — каждые {int(settings.default_check_interval_hours)} ч. "
            f"До {int(settings.max_stories_per_user)} наблюдений на человека. "
            f"Ручных проверок — до {int(settings.max_manual_checks_per_day)} в сутки; "
            f"между ними минимум {int(settings.manual_check_cooldown_seconds)} сек.\n\n"
            + INTENSIVE_HELP + "\n"
            "Время считается от включения режима, а не от предыдущей проверки. На паузе срок продолжает идти, "
            "и тема занимает место до выключения, переноса или окончания режима. "
            "Ручная проверка не откладывает автоматическую. /report — время ежедневного отчёта по Москве.\n\n"
            "/watching — список, история, пауза и удаление\n"
            "/journal — все отправленные уведомления с выбором периода\n"
            "/news — все присланные новости, отложенные карточки и повтор обработки\n"
            "/menu — главное меню из любого раздела\n"
            "/interests — ваши интересы для будущих подборок; просмотр и удаление\n"
            "/check_now — проверить выбранное наблюдение\n"
            "/cancel — как отменить создание\n\n"
            "Поиск может пропускать публикации, а ИИ — ошибаться. Сверяйте важные выводы с источниками. "
            "Не отправляйте пароли и личные документы. Удаление наблюдения удаляет его сохранённые тексты и историю. "
            "Присланная новость остаётся в /news, а отмеченный интерес — в /interests; их можно убрать отдельно. Подборки пока не запущены.")

    @router.message(Command("interests"), ~F.forward_origin)
    async def interests_command(message: Message) -> None:
        await show_interests(message, message.from_user.id)

    @router.message(Command("watching"), ~F.forward_origin)
    async def watching(message: Message) -> None:
        await show_list(message, message.from_user.id)

    @router.message(Command("cancel"), ~F.forward_origin)
    async def cancel(message: Message) -> None:
        await _answer(message, "Нажмите «Решить позже» под карточкой: новость останется в /news, наблюдение не включится. "
            "Для удаления из списка откройте новость и выберите «Убрать из новостей». "
            "На старых карточках доступна «Отмена». Можно сразу прислать другую тему.")

    @router.message(Command("check_now"), ~F.forward_origin)
    async def check_now(message: Message, command: CommandObject) -> None:
        story_id = _command_id(command)
        if story_id is None:
            await show_list(message, message.from_user.id, "check")
        else:
            await run_manual_check(message, message.from_user.id, story_id)

    @router.message(Command("admin"), ~F.forward_origin)
    async def admin(message: Message) -> None:
        await show_admin(message, message.from_user.id, replace=False)

    @router.message(F.text.lower().in_({'admin', 'админ'}), ~F.forward_origin)
    async def admin_word(message: Message):
        await show_admin(message, message.from_user.id, replace=False)

    async def show_admin_stats(message, user_id):
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
        await _replace(message, text, admin_keyboard())

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

    async def offer_report_time(message, user_id):
        if await service.repo.claim_report_prompt(user_id) is not None:
            try:
                await _answer(message, REPORT_PROMPT, report_keyboard())
            except Exception:
                await service.repo.reset_report_prompt(user_id)
                raise

    @router.message(Command('report'), ~F.forward_origin)
    async def report_command(message: Message, command: CommandObject):
        if command.args:
            await service.repo.set_report_time(message.from_user.id, command.args.strip())
            await _answer(message, f'✅ Ежедневный отчёт — в {escaped(command.args.strip(), 10)} МСК. '
                          'Изменить время: /report.', report_keyboard())
        else:
            pref = await service.repo.report_preference(message.from_user.id)
            current = (f'Сейчас: {pref.minute // 60:02d}:{pref.minute % 60:02d} МСК.\n\n'
                       if pref and pref.minute is not None else '')
            await _answer(message, current + REPORT_PROMPT, report_keyboard())

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        data = query.data or ''
        uid = query.from_user.id
        if data == 'add:news':
            await query.answer()
            await _replace(query.message, '➕ <b>Добавить новость</b>\n\nПришлите ссылку, текст или '
                'перешлите пост из канала. Можно написать тему своими словами. '
                'Сохраню новость и сообщу, когда можно выбрать наблюдение.', main_keyboard())
            return
        if data.startswith('admin:'):
            if not await service.is_admin(uid):
                raise UserError('Это действие доступно только администратору.')
            await query.answer()
            action = data.split(':', 1)[1]
            if action == 'home':
                await show_admin(query.message, uid)
            elif action == 'stats':
                await show_admin_stats(query.message, uid)
            elif action == 'errors':
                await _replace(query.message, escaped(await service.admin_errors(uid), 3300), admin_keyboard())
            elif action == 'plans':
                confirmations.pop(uid, None)
                await show_catalog(query.message, uid, admin=True)
            elif action == 'demo':
                await _replace(query.message, escaped(await service.demo_update(uid), 1800), admin_keyboard())
            elif action == 'web':
                await _replace(query.message, '📣 <b>Веб-админка</b>\n\n'
                    '«Оповещения»: новости бота, рекламные предложения и скидки, предпросмотр и тестовая отправка.\n'
                    '«Тарифы и кредиты»: цены, лимиты, назначение тарифа, баланс и роли.\n'
                    'Доступ — по отдельному логину и паролю администратора.', admin_keyboard())
            return
        if data.startswith('settings:'):
            await query.answer()
            if data == 'settings:home':
                confirmations.pop(uid, None)
                await show_settings(query.message, uid)
            elif data in {'settings:ads:0', 'settings:ads:1'}:
                await service.commerce.set_promotions(uid, data.endswith(':1'))
                await show_settings(query.message, uid)
            elif data == 'settings:reset':
                token = confirmation(uid, 'reset')
                await _replace(query.message, '🗑 <b>Начать заново?</b>\n\n'
                    'Будут удалены все ваши новости, наблюдения, интересы, история обновлений и время отчёта. '
                    'Восстановить их нельзя. Тариф, баланс и доступ сохранятся.\n\n'
                    'Уже отправленные сообщения останутся в чате.', _keyboard(
                    [InlineKeyboardButton(text='Да, удалить мои новости и темы', callback_data=f'settings:confirm:{token}')],
                    [InlineKeyboardButton(text='Отмена', callback_data='settings:home')]))
            elif data.startswith('settings:confirm:'):
                consume_confirmation(uid, data.rsplit(':', 1)[1], 'reset')
                await service.repo.reset_content(uid)
                await _replace(query.message, '✅ Ваши новости и темы очищены. Пришлите новую новость, '
                    'чтобы начать заново. Тариф и баланс сохранены.', main_keyboard())
            return
        if data.startswith('admplan:') or data.startswith('admconfirm:'):
            if not await service.is_admin(uid):
                raise UserError('Это действие доступно только администратору.')
            await query.answer()
            if data.startswith('admconfirm:'):
                token = data.split(':', 1)[1]
                plan_id = consume_confirmation(uid, token, 'plan')
                await service.commerce.admin_self_plan(uid, plan_id, f'self-plan:{uid}:{token}')
                await _replace(query.message, '✅ Тестовый тариф обновлён.\n\n' + await service.account_text(uid), admin_keyboard())
            else:
                raw = data.split(':', 1)[1]
                if not raw.isascii() or not raw.isdigit() or len(raw) > 10:
                    raise UserError('Кнопка устарела.')
                plan_id = int(raw)
                chosen = next((p for p in await service.commerce.catalog() if p.id == plan_id), None)
                if plan_id and chosen is None:
                    raise UserError('Тариф недоступен.')
                token = confirmation(uid, 'plan', plan_id)
                await _replace(query.message, f'Подключить себе тестовый тариф «{escaped(chosen.name, 160)}» без оплаты?' if chosen else
                    'Снять свой тариф и вернуться к базовым возможностям?', _keyboard(
                    [InlineKeyboardButton(text='Подтвердить', callback_data=f'admconfirm:{token}')],
                    [InlineKeyboardButton(text='Отмена', callback_data='admin:plans')]))
            return
        if data.startswith('shop:'):
            await query.answer()
            raw = data.split(':', 1)[1]
            if raw == 'home':
                await show_catalog(query.message, uid)
            elif raw.isascii() and raw.isdigit() and len(raw) <= 10:
                plan = next((p for p in await service.commerce.catalog() if p.id == int(raw)), None)
                if not plan:
                    raise UserError('Тариф больше недоступен.')
                await _replace(query.message, f'💎 <b>{escaped(plan.name, 160)}</b>\n\n'
                    f'Цена: {plan.price_minor / 100:.2f} {escaped(plan.currency, 10)} / {plan.period_days} дней\n'
                    f'Тем одновременно: {plan.stories}\nСрочных тем: {plan.intensive_slots}\n'
                    f'Ручных проверок в сутки: {plan.manual_daily}\n'
                    f'Обсуждение: {"включено" if plan.discussion else "не включено"}\n\n'
                    f'Кредиты за действие: разбор {plan.news_credits}, проверка {plan.check_credits}, '
                    f'обсуждение {plan.discussion_credits}.\n\nОнлайн-оплата пока в разработке.', _keyboard(
                    [InlineKeyboardButton(text='🛒 Купить — скоро', callback_data='purchase:stub')],
                    [InlineKeyboardButton(text='← Все тарифы', callback_data='shop:home')]))
            return
        if data == 'purchase:stub':
            await query.answer('Онлайн-оплата пока в разработке. Деньги не списаны.', show_alert=True)
            return
        if (query.data or '').startswith('report:'):
            await query.answer()
            value = query.data.split(':', 1)[1]
            if value == 'settings':
                pref = await service.repo.report_preference(query.from_user.id)
                current = (f'Сейчас: {pref.minute // 60:02d}:{pref.minute % 60:02d} МСК.\n\n'
                           if pref and pref.minute is not None else '')
                await _answer(query.message, current + REPORT_PROMPT, report_keyboard())
            else:
                await service.repo.set_report_time(query.from_user.id, value)
                await _replace(query.message, f'✅ Ежедневный отчёт — в {escaped(value, 10)} МСК. '
                               'Все обычные подписки собраны вместе. Изменить время: /report.')
            return
        full_match = re.fullmatch(r'full:([1-9][0-9]{0,9})', query.data or '')
        if full_match:
            await query.answer()
            pair = await service.repo.get_full_update(query.from_user.id, int(full_match[1]))
            if pair is None:
                await _answer(query.message, 'Обновление удалено или недоступно.')
                return
            update, story = pair
            pages = full_update_pages(story, update)
            for index, page in enumerate(pages, 1):
                prefix = f'📄 {index}/{len(pages)}\n\n' if len(pages) > 1 else ''
                await _answer(query.message, prefix + page,
                              _keyboard([_button('📰 К новости', 'story', story.id)]))
            return
        if query.data in {'account:home', 'account:prices', 'account:history', 'account:manage'}:
            await query.answer()
            view = query.data.split(':')[1]
            await _replace(query.message, await service.account_text(query.from_user.id, view), account_keyboard(view))
            return
        if query.data == 'menu:0':
            await query.answer()
            # Keep action/progress cards intact while ongoing edits finish.
            await show_main(query.message)
            return
        if query.data == 'help:0':
            await query.answer()
            await help_command(query.message)
            return
        news_match = re.fullmatch(r'(news|nopen|nretry|nwatch|nfocus|ninterest|nlater|ninput|ndelete|ndelete_yes):([0-9]{1,10})', query.data or '')
        if news_match:
            news_action, raw_news_id = news_match.groups()
            news_id = int(raw_news_id)
            if news_id > 2_147_483_647 or (news_id == 0 and news_action != 'news'):
                await query.answer('Кнопка устарела. Откройте /news.', show_alert=True)
                return
            if news_action == 'news':
                await query.answer()
                await show_news(query.message, query.from_user.id, news_id, replace=True)
                return
            item = await service.get_user_news(query.from_user.id, news_id)
            if item is None:
                await query.answer('Новость удалена или недоступна.', show_alert=True)
                return
            if news_action == 'ninterest':
                result = await service.user_news_interest(query.from_user.id, news_id)
                await query.answer('Интерес сохранён.' if result.created else 'Уже в ваших интересах.')
                prefix = {'active': 'Наблюдение продолжает работать.\n\n',
                          'paused': 'Наблюдение остаётся на паузе.\n\n'}.get(result.monitoring_status, 'Наблюдение не включено.\n\n')
                await _replace(query.message, prefix + interest_text(result.interest), interest_keyboard(result.interest))
                return
            if news_action in {'nwatch', 'nfocus'}:
                linked = await service.user_news_story(query.from_user.id, news_id)
                await service.defer_user_news(query.from_user.id, news_id)
                parsed = (news_action[1:], linked.id)
                transfer = None
            else:
                await query.answer()
                if news_action == 'nopen':
                    await show_news_item(query.message, query.from_user.id, item)
                elif news_action == 'nretry':
                    if item.status == 'ready':
                        await show_news_item(query.message, query.from_user.id, item)
                    else:
                        await process_news(query.message, query.from_user.id, item)
                elif news_action == 'nlater':
                    await service.defer_user_news(query.from_user.id, news_id)
                    await _replace(query.message, '📥 Новость сохранена. Вернитесь к ней в «Новости пользователя», когда захотите.',
                        _keyboard([InlineKeyboardButton(text='📥 Открыть новость', callback_data=f'nopen:{news_id}')]))
                elif news_action == 'ninput':
                    keyboard = _keyboard([InlineKeyboardButton(text='← К карточке', callback_data=f'nopen:{news_id}')])
                    if _units(html.escape(item.original_text)) > 3300:
                        await query.message.answer_document(BufferedInputFile(item.original_text.encode('utf-8'), filename=f'news-{item.id}.txt'),
                            caption='Полный присланный текст новости.', reply_markup=keyboard)
                    else:
                        await query.message.answer('📄 <b>Присланная новость</b>\n\n' + html.escape(item.original_text),
                            parse_mode='HTML', reply_markup=keyboard,
                            reply_parameters=ReplyParameters(message_id=item.input_message_id, allow_sending_without_reply=True) if item.input_message_id else None)
                elif news_action == 'ndelete':
                    await _replace(query.message, 'Убрать эту новость из «Новости пользователя»? Сохранённый текст и карточка будут удалены. '
                        'Если включено наблюдение, оно продолжится; его можно удалить отдельно.',
                        _keyboard([InlineKeyboardButton(text='🗑 Да, убрать', callback_data=f'ndelete_yes:{news_id}'),
                                   InlineKeyboardButton(text='Оставить', callback_data=f'nopen:{news_id}')]))
                elif news_action == 'ndelete_yes':
                    await service.delete_user_news(query.from_user.id, news_id)
                    await show_news(query.message, query.from_user.id, replace=True)
                return
        else:
            transfer = parse_transfer(query.data)
            parsed = parse_callback(query.data)
        if (query.data or '').startswith(('jp:', 'jn:', 'ju:', 'jo:')):
            await journal_callback(query)
            return
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
                    "⚡ <b>Слоты срочных наблюдений заняты</b>.\n\n"
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
            await offer_report_time(message, user_id)
            return
        if action == "cancel":
            await query.answer()
            await service.cancel_draft(user_id, object_id)
            await _replace(message, "Создание наблюдения отменено. Сохранённые новости доступны в «Новости пользователя».")
            return
        story = await service.get_story(user_id, object_id)
        if story is None or story.status not in {"active", "paused"}:
            await query.answer("Наблюдение больше недоступно. Откройте /watching.", show_alert=True)
            return
        if action == "chat":
            await query.answer()
            await _answer(message, f'Чтобы обсудить эту новость, отправьте:\n/discuss {story.id} ваш вопрос\n\n'
                'Ответ опирается на сохранённую новость, без нового поиска. Доступ и цена: /account.')
            return
        await query.answer()
        if action == "story":
            await _answer(message, story_text(story), story_keyboard(story))
        elif action == "daily":
            changed = await service.set_monitoring_mode(user_id, object_id, "daily")
            await _replace(message, "✅ Новость включена в ежедневный отчёт.\n\n" + story_text(changed), story_keyboard(changed))
            await offer_report_time(message, user_id)
        elif action == "check":
            await run_manual_check(message, user_id, object_id)
        elif action in {"pause", "resume"}:
            changed = await service.set_status(user_id, object_id, "paused" if action == "pause" else "active")
            if changed is None:
                raise UserError("Наблюдение больше недоступно. Откройте /watching.")
            await _answer(message, story_text(changed), story_keyboard(changed))
            if action == 'resume' and getattr(changed, 'monitoring_mode', 'daily') == 'daily':
                await offer_report_time(message, user_id)
        elif action == "delete":
            await _answer(message,
                f"Удалить наблюдение «{escaped(story.title, 200)}»?\n\nЕго тексты, источники и история будут удалены. Это действие нельзя отменить. "
                "Присланная новость останется в /news, отмеченный интерес — в /interests; их можно убрать отдельно.",
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

    @router.message(Command('account'), ~F.forward_origin)
    async def account_command(message: Message):
        await _answer(message, await service.account_text(message.from_user.id), account_keyboard())

    @router.message(Command('settings'), ~F.forward_origin)
    async def settings_command(message: Message):
        await show_settings(message, message.from_user.id, replace=False)

    @router.message(Command('discuss'), ~F.forward_origin)
    async def discuss_command(message: Message):
        parts = (message.text or '').split(maxsplit=2)
        if len(parts) != 3 or not parts[1].isdigit():
            await _answer(message, 'Формат: /discuss ID_наблюдения ваш вопрос. Список: /watching')
            return
        answer = await service.discuss(message.from_user.id, int(parts[1]), parts[2])
        await _answer(message, escaped(answer, 2800))

    @router.message(F.text.startswith("/"), ~F.forward_origin)
    async def unknown_command(message: Message) -> None:
        await _answer(message, "Не знаю эту команду. /help — подсказка, /watching — ваши наблюдения. Для нового сюжета просто пришлите ссылку или текст.")

    @router.message(F.text | F.caption)
    async def new_story(message: Message, album_messages: list[Message] | None = None) -> None:
        seed = extract_story_input(message, album_messages)
        item = await service.save_user_news(message.from_user.id, seed.text, source_url=seed.source_url,
            use_text=seed.use_text, input_message_id=message.message_id)
        if item.status == 'ready':
            await show_news_item(message, message.from_user.id, item, replace=False)
        else:
            await process_news(message, message.from_user.id, item)

    @router.message()
    async def unsupported(message: Message) -> None:
        await _answer(message, "В этом сообщении нет текста новости. Перешлите пост с текстом или подписью к фото/видео либо кратко опишите событие. Содержимое самих фото, видео и голосовых пока не распознаю.")

    return router
