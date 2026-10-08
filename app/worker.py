import asyncio
import logging
import time
import tempfile
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, ReplyParameters
from app.bot import escaped, notification_text, notification_keyboard
from app.user_news import ready_text, keyboard as news_keyboard
from app.daily_reports import REPORT_PROMPT, report_keyboard
from app.journal import MSK
from app.navigation import with_home
from app.announcements import Announcements, announcement_text
from app.report_controls import DEFAULTS, ReportControls, clip_words

log = logging.getLogger(__name__)


async def display_options(service, uid):
    commerce = getattr(service, 'commerce', None)
    enabled = bool((await commerce.snapshot(uid))['limits'].get('full_reports', False)) if commerce else False
    sessions = getattr(service.repo, 'session_factory', None)
    options = (await ReportControls(sessions).snapshot())['values'] if sessions else dict(DEFAULTS)
    return enabled, options

async def deliver_news_ready(service, bot):
    for item in await service.repo.pending_news_notices():
        success = False
        retry_after = 0
        try:
            fresh = await service.repo.get_user_news(item.user_id, item.id)
            if fresh and fresh.status == 'ready' and not fresh.notice_suppressed and fresh.notice_sent_at is None:
                await bot.send_message(item.user_id, ready_text(fresh), parse_mode='HTML',
                    reply_markup=news_keyboard(fresh), disable_notification=False, request_timeout=30,
                    reply_parameters=ReplyParameters(message_id=fresh.input_message_id,
                        allow_sending_without_reply=True) if fresh.input_message_id else None)
                success = True
        except TelegramForbiddenError:
            await service.repo.record_product_event('delivery_forbidden', item.user_id, f'forbidden:news:{item.id}')
            await service.repo.defer_user_news(item.user_id, item.id, reason='delivery_forbidden')
        except TelegramRetryAfter as exc:
            retry_after = exc.retry_after
        except Exception as exc:
            await service._error('news_ready_notification', exc, user_id=item.user_id)
        await service.repo.mark_news_notice(item.user_id, item.id, item.notice_token, success, retry_after=retry_after)

async def deliver_notifications(service, bot):
    for update, story in await service.repo.pending_notifications(limit=1):
        success = False
        message_id = None
        try:
            # Recheck immediately before delivering to respect pause/delete.
            fresh = await service.repo.get_story(story.user_id, story.id)
            if fresh and fresh.status == 'active' and (
                getattr(fresh, 'monitoring_mode', 'intensive') == 'intensive' or getattr(update, 'is_demo', False)
            ):
                enabled, options = await display_options(service, story.user_id)
                message = await bot.send_message(story.user_id, notification_text(story, update, options['preview_words']),
                    reply_markup=notification_keyboard(story, update, full_reports=enabled), request_timeout=30)
                message_id = message.message_id
                success = True
        except TelegramForbiddenError:
            await service.repo.record_product_event('delivery_forbidden', story.user_id, f'forbidden:update:{update.id}')
            await service.repo.set_status(story.user_id, story.id, 'paused', reason='delivery_forbidden')
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 30))
        except Exception as exc:
            await service._error('notification', exc, story_id=story.id, user_id=story.user_id)
        await service.repo.mark_notified(update.id, success, delivery_token=update.delivery_lock_token,
                                        telegram_message_id=message_id)


def daily_report_page(entries, cutoff, index, count, full_reports=False, word_limit=40):
    heading = f'🗓 <b>Ежедневный отчёт · {cutoff.astimezone(MSK):%d.%m.%Y}</b>'
    if count > 1:
        heading += f' · {index}/{count}'
    lines, rows = [heading], []
    for entry in entries:
        lines.append(f'<b>{escaped(entry["title"], 160)}</b>')
        updates = entry['updates']
        if updates:
            for item in updates[-2:]:
                label = 'Уточнение' if item['kind'] == 'context' else 'Новое'
                lines.append(f'• {label}: {escaped(clip_words(item["summary"], word_limit), 350)}')
            if len(updates) > 2:
                lines.append(f'Ещё обновлений: {len(updates) - 2}. Все — в истории новости.')
            rows.append([InlineKeyboardButton(text=f'{"📖 Читать дальше" if full_reports else "🔒 Полный отчёт · подписка"} · {entry["title"][:30]}',
                        callback_data=f'full:{updates[-1]["id"]}')])
            rows.append([InlineKeyboardButton(text='🕒 Все обновления этой новости',
                        callback_data=f'history:{entry["story_id"]}')])
        else:
            lines.append('— Без изменений' if entry['checked'] else '⏳ Нет свежей проверки')
    lines.append('Время отчёта по Москве: /report')
    return '\n\n'.join(lines), with_home(rows)


async def deliver_daily_reports(service, bot):
    pref = await service.repo.claim_daily_report()
    if pref is None:
        return
    try:
        chunks = [pref.payload[i:i+3] for i in range(0, len(pref.payload), 3)]
        for index in range(pref.sent_parts, len(chunks)):
            entries = []
            for entry in chunks[index]:
                fresh = await service.repo.get_story(pref.user_id, entry['story_id'])
                if fresh and fresh.status == 'active' and fresh.monitoring_mode == 'daily':
                    entries.append(entry)
            message_id = None
            if entries:
                enabled, options = await display_options(service, pref.user_id)
                text, keyboard = daily_report_page(entries, pref.cutoff, index+1, len(chunks), enabled, options['digest_words'])
                message = await bot.send_message(pref.user_id, text, parse_mode='HTML',
                    reply_markup=keyboard, disable_web_page_preview=True, request_timeout=30)
                message_id = message.message_id
            saved = await service.repo.advance_daily_report(pref.user_id, pref.token, sent_parts=index+1,
                update_ids=[u['id'] for e in entries for u in e['updates']], message_id=message_id)
            if not saved:
                return
        await service.repo.advance_daily_report(pref.user_id, pref.token)
    except TelegramForbiddenError:
        await service.repo.advance_daily_report(pref.user_id, pref.token, forbidden=True)
    except TelegramRetryAfter as exc:
        await service.repo.advance_daily_report(pref.user_id, pref.token, retry_after=exc.retry_after)
    except Exception as exc:
        await service.repo.advance_daily_report(pref.user_id, pref.token, retry_after=60)
        await service._error('daily_report', exc, user_id=pref.user_id)


async def deliver_report_prompt(service, bot):
    owner = await service.repo.claim_report_prompt()
    if owner is None:
        return
    try:
        await bot.send_message(owner, REPORT_PROMPT, reply_markup=report_keyboard(), request_timeout=30)
    except TelegramForbiddenError:
        for story in await service.repo.list_stories(owner):
            if story.status == 'active' and story.monitoring_mode == 'daily':
                await service.repo.set_status(owner, story.id, 'paused', reason='delivery_forbidden')
    except Exception:
        await service.repo.reset_report_prompt(owner)
        raise


async def deliver_announcements(service, bot, limit=10):
    queue = Announcements(service.repo.session_factory)
    deadline = time.monotonic() + 20
    for _ in range(limit):
        if time.monotonic() >= deadline:
            return
        pair = await queue.claim()
        if pair is None:
            return
        delivery, campaign = pair
        if not await queue.may_send(delivery.id, delivery.token):
            await queue.finish(delivery.id, delivery.token, 'cancelled')
            continue
        keyboard = None
        if campaign.button == 'report':
            keyboard = with_home([[InlineKeyboardButton(text='🕒 Выбрать время отчёта', callback_data='report:settings')]])
        elif campaign.button == 'menu':
            keyboard = with_home()
        elif campaign.button == 'buy':
            account = await service.commerce.snapshot(delivery.user_id)
            if account['account'] and not account['account'].promotions_enabled:
                await queue.finish(delivery.id, delivery.token, 'cancelled')
                continue
            keyboard = with_home([[InlineKeyboardButton(text='🛍 Посмотреть тарифы / купить', callback_data='shop:home')],
                [InlineKeyboardButton(text='⚙️ Настройки предложений', callback_data='settings:home')]])
        try:
            message = await bot.send_message(delivery.user_id, announcement_text(campaign), parse_mode=None,
                reply_markup=keyboard, disable_web_page_preview=True, request_timeout=30)
            await queue.finish(delivery.id, delivery.token, 'sent', message_id=message.message_id)
        except TelegramForbiddenError:
            await queue.finish(delivery.id, delivery.token, 'blocked', error='bot_blocked')
        except TelegramRetryAfter as exc:
            await queue.finish(delivery.id, delivery.token, 'retry', error='rate_limit', retry_after=exc.retry_after)
            # Respect Telegram's backoff for the entire announcement queue.
            return
        except TelegramBadRequest:
            await queue.finish(delivery.id, delivery.token, 'permanent', error='telegram_rejected')
        except Exception as exc:
            await queue.finish(delivery.id, delivery.token, 'retry', error=type(exc).__name__[:64])
            await service._error('announcement_delivery', exc, user_id=delivery.user_id)
        await asyncio.sleep(0.1)

async def run_worker(service, bot):
    heartbeat = Path(tempfile.gettempdir()) / 'newswatch-worker-heartbeat'
    check_tasks: set[asyncio.Task] = set()
    try:
        while True:
            heartbeat.write_text(str(time.time()))
            try:
                await deliver_announcements(service, bot)
                await deliver_notifications(service, bot)
                await deliver_news_ready(service, bot)
                await deliver_report_prompt(service, bot)
                await deliver_daily_reports(service, bot)
                if service.provider_ready() and len(check_tasks) < 2:
                    for story_id in await service.repo.due_story_ids(limit=2-len(check_tasks)):
                        story = await service.repo.claim_story(story_id)
                        if story:
                            task = asyncio.create_task(service.check_story(story))
                            check_tasks.add(task)
                            task.add_done_callback(check_tasks.discard)
            except Exception as exc:
                await service._error('worker', exc)
            await asyncio.sleep(10)
    finally:
        for task in check_tasks:
            task.cancel()
        await asyncio.gather(*check_tasks, return_exceptions=True)
