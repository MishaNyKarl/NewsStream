"""Shared escape route for every inline screen, including progress and notices."""
from typing import Any
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def with_home(rows=()):
    clean = [[b for b in row if b.callback_data != 'menu:0'] for row in rows]
    clean = [row for row in clean if row]
    return InlineKeyboardMarkup(inline_keyboard=clean + [[
        InlineKeyboardButton(text='🏠 Главное меню', callback_data='menu:0')]])


def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='📥 Новости пользователя', callback_data='news:0'),
         InlineKeyboardButton(text='🗂 Журнал', callback_data='jp:week')],
        [InlineKeyboardButton(text='📋 Мои наблюдения', callback_data='list:0'),
         InlineKeyboardButton(text='⭐ Мои интересы', callback_data='interests:0')],
        [InlineKeyboardButton(text='❔ Как пользоваться', callback_data='help:0')]])


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

