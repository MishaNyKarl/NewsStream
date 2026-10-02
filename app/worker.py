import asyncio
import logging
import time
import tempfile
from pathlib import Path

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import ReplyParameters
from app.bot import notification_text, notification_keyboard
from app.user_news import ready_text, keyboard as news_keyboard

log = logging.getLogger(__name__)

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
            if fresh and fresh.status == 'active':
                message = await bot.send_message(story.user_id, notification_text(story, update),
                    reply_markup=notification_keyboard(story, update), request_timeout=30)
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

async def run_worker(service, bot):
    heartbeat = Path(tempfile.gettempdir()) / 'newswatch-worker-heartbeat'
    check_tasks: set[asyncio.Task] = set()
    try:
        while True:
            heartbeat.write_text(str(time.time()))
            try:
                await deliver_notifications(service, bot)
                await deliver_news_ready(service, bot)
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
