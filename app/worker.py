import asyncio
import logging
import time
import tempfile
from pathlib import Path

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from app.bot import notification_text, notification_keyboard

log = logging.getLogger(__name__)

async def deliver_notifications(service, bot):
    for update, story in await service.repo.pending_notifications(limit=1):
        success = False
        try:
            # Recheck immediately before delivering to respect pause/delete.
            fresh = await service.repo.get_story(story.user_id, story.id)
            if fresh and fresh.status == 'active':
                await bot.send_message(story.user_id, notification_text(story, update),
                    reply_markup=notification_keyboard(story, update), request_timeout=30)
                success = True
        except TelegramForbiddenError:
            await service.repo.set_status(story.user_id, story.id, 'paused')
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 30))
        except Exception as exc:
            await service._error('notification', exc, story_id=story.id, user_id=story.user_id)
        await service.repo.mark_notified(update.id, success, delivery_token=update.delivery_lock_token)

async def run_worker(service, bot):
    heartbeat = Path(tempfile.gettempdir()) / 'newswatch-worker-heartbeat'
    check_tasks: set[asyncio.Task] = set()
    try:
        while True:
            heartbeat.write_text(str(time.time()))
            try:
                await deliver_notifications(service, bot)
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
