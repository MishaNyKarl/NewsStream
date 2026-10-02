"""Refresh profiles in the bot process; never expose Telegram file URLs/tokens."""
import asyncio
import logging
from datetime import timedelta
from io import BytesIO

from sqlalchemy import or_, select

from app.models import User, UserProfile, utcnow


async def refresh_profile(bot, sessions, uid):
    name = username = avatar = None
    try:
        chat = await bot.get_chat(uid, request_timeout=10)
        name = ' '.join(x for x in (chat.first_name, chat.last_name) if x)[:512] or None
        username = chat.username
        photos = await bot.get_user_profile_photos(uid, limit=1, request_timeout=10)
        if photos.photos:
            photo = min(photos.photos[0], key=lambda p: p.width*p.height)
            if photo.file_size is not None and photo.file_size <= 262144:
                stream = BytesIO()
                await bot.download(photo.file_id, destination=stream, timeout=10)
                content = stream.getvalue()
                if len(content) <= 262144 and content.startswith(b'\xff\xd8\xff'):
                    avatar = content
    except Exception as exc:
        logging.getLogger(__name__).info('profile_refresh_unavailable type=%s', type(exc).__name__)
    async with sessions.begin() as session:
        row = await session.get(UserProfile, uid)
        if row is None:
            row = UserProfile(user_id=uid)
            session.add(row)
        # Clear unavailable thumbnails rather than keep a removed/private photo.
        row.display_name, row.username, row.avatar, row.checked_at = name, username, avatar, utcnow()


async def refresh_profiles(bot, sessions):
    while True:
        try:
            async with sessions() as session:
                ids = list(await session.scalars(select(User.telegram_id).outerjoin(
                    UserProfile, UserProfile.user_id == User.telegram_id).where(or_(
                        UserProfile.user_id.is_(None), UserProfile.checked_at < utcnow()-timedelta(hours=24)))
                    .order_by(UserProfile.checked_at.asc().nullsfirst(), User.telegram_id).limit(10)))
            for uid in ids:
                await refresh_profile(bot, sessions, uid)
                await asyncio.sleep(2)
        except Exception as exc:
            logging.getLogger(__name__).warning('profile_refresh_failed type=%s', type(exc).__name__)
        await asyncio.sleep(60)
