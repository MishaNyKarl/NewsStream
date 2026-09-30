"""Normalize Telegram text, captions and forwarded albums without reading media."""
import asyncio
import re
import time
from dataclasses import dataclass

from aiogram import BaseMiddleware
from aiogram.types import Message


@dataclass(frozen=True)
class StoryInput:
    text: str
    source_url: str | None = None
    use_text: bool = False


def public_forward_url(message):
    origin = message.forward_origin
    if origin is None or origin.type != 'channel':
        return None
    username = origin.chat.username
    if username and re.fullmatch(r'[A-Za-z0-9_]{1,64}', username) and origin.message_id > 0:
        return f'https://t.me/{username}/{origin.message_id}'
    return None


def extract_story_input(message, album_messages=None):
    messages = album_messages or [message]
    pieces = []
    for item in sorted(messages, key=lambda m: m.message_id):
        body = (item.text or item.caption or '').strip()
        if body and body not in pieces:
            pieces.append(body)
    body = '\n\n'.join(pieces)
    forwarded = any(item.forward_origin is not None for item in messages)
    captioned = any(item.caption for item in messages)
    source = next((url for item in messages if (url := public_forward_url(item))), None)
    # Forwarded prose/captions are primary content. An unrelated channel/footer
    # URL must not force web fetching or make the entire post unreadable.
    return StoryInput(body, source, use_text=forwarded or captioned)


class AlbumMiddleware(BaseMiddleware):
    """Collect a Telegram album before per-message throttling; emit one news item."""
    def __init__(self, wait_seconds=0.8, clock=time.monotonic):
        self.wait_seconds = wait_seconds
        self.clock = clock
        self.pending = {}
        self.seen = {}

    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not event.media_group_id or event.chat.type != 'private':
            return await handler(event, data)
        if event.from_user is None or event.from_user.is_bot:
            return None
        now = self.clock()
        self.seen = {key: state for key, state in self.seen.items() if now-state[0] < 60}
        key = (event.chat.id, event.from_user.id, event.media_group_id)
        if key in self.seen:
            if self.seen[key][1] or not (event.text or event.caption):
                return None
            # A caption arriving unusually late must still be accepted even if
            # the initial captionless group already received a help message.
            self.seen.pop(key)
        if key in self.pending:
            if len(self.pending[key]) < 10:
                self.pending[key].append(event)
            return None
        if len(self.pending) >= 100:
            return None
        self.pending[key] = [event]
        try:
            await asyncio.sleep(self.wait_seconds)
            if not any(item.text or item.caption for item in self.pending[key]):
                await asyncio.sleep(self.wait_seconds * 2)
            messages = self.pending.pop(key)
            self.seen[key] = (self.clock(), any(item.text or item.caption for item in messages))
            representative = next((item for item in messages if item.text or item.caption), messages[0])
            data['album_messages'] = messages
            return await handler(representative, data)
        finally:
            self.pending.pop(key, None)
