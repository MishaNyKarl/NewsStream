import asyncio
import logging
import sys
import time
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, LinkPreviewOptions
from app.bot import build_router
from app.config import get_settings
from app.db import close_db
from app.service import BotService
from app.worker import run_worker

async def bot_heartbeat():
    while True:
        (Path(tempfile.gettempdir()) / 'newswatch-bot-heartbeat').write_text(str(time.time()))
        await asyncio.sleep(20)

async def main(role='bot'):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    for name in ['httpx', 'httpcore', 'aiogram.event']:
        logging.getLogger(name).setLevel(logging.WARNING)
    service = BotService(settings)
    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)))
    heartbeat_task = None
    try:
        if role == 'worker':
            await run_worker(service, bot)
        else:
            try:
                await bot.set_my_commands([
                    BotCommand(command='start', description='Как работает бот'),
                    BotCommand(command='menu', description='Главное меню'),
                    BotCommand(command='news', description='Новости пользователя'),
                    BotCommand(command='watching', description='Мои наблюдения'),
                    BotCommand(command='journal', description='Журнал уведомлений'),
                    BotCommand(command='interests', description='Мои интересы'),
                    BotCommand(command='check_now', description='Проверить сюжет'),
                    BotCommand(command='help', description='Помощь'),
                    BotCommand(command='admin', description='Управление тестом'),
                ], request_timeout=15)
                await bot.set_my_name(name='Развитие новостей', request_timeout=15)
                await bot.set_my_description(description='Пришлите новость, ссылку или тему — бот будет следить за развитием этой истории и сообщать о существенных изменениях. Закрытый тест: вход по приглашению.', request_timeout=15)
                await bot.set_my_short_description(short_description='Наблюдение за развитием конкретных историй. Существенные обновления и ссылки на источники.', request_timeout=15)
            except TelegramAPIError as exc:
                logging.getLogger(__name__).warning('Profile setup skipped: %s', type(exc).__name__)
            dp = Dispatcher()
            dp.include_router(build_router(service, settings))
            heartbeat_task = asyncio.create_task(bot_heartbeat())
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(),
                handle_signals=True, tasks_concurrency_limit=20)
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        await service.close()
        await bot.session.close()
        await close_db()

if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else 'bot'))
