"""Saved-news cards and choices; no storage/network calls during rendering."""
import html
from datetime import datetime, timezone
from types import SimpleNamespace

from aiogram.types import InlineKeyboardButton
from app.navigation import safe_url, with_home


def preview(item, frequency=24):
    data = item.parsed_data or {}
    return SimpleNamespace(id=item.id, title=data.get('title', 'Новость'),
        summary=data.get('short_summary', ''), current_state=data.get('current_state', ''),
        watch_goals=data.get('watch_goals', []), original_url=item.source_url,
        check_frequency_hours=frequency, status='draft')


def label(item):
    if item.status == 'processing':
        if item.processing_until and item.processing_until > datetime.now(timezone.utc):
            return '⏳ Обрабатывается'
        return '⚠️ Обработка прервалась'
    return {'ready': '✅ Обработана', 'failed': '⚠️ Не обработана', 'pending': '📥 Сохранена'}.get(item.status, '📥 Сохранена')


def keyboard(item, story=None):
    def button(text, action):
        return InlineKeyboardButton(text=text, callback_data=f'n{action}:{item.id}')
    rows = []
    if item.status == 'ready':
        if story and story.status in {'active', 'paused'}:
            rows.append([InlineKeyboardButton(text='📋 Открыть наблюдение', callback_data=f'story:{story.id}')])
        else:
            rows.extend([[button('✅ Следить', 'watch')], [button('⚡ Следить внимательнее', 'focus')]])
        rows.append([button('⭐ Просто интересна тема', 'interest')])
        rows.append([button('📥 Решить позже', 'later')])
    elif item.status != 'processing' or not item.processing_until or item.processing_until <= datetime.now(timezone.utc):
        rows.append([button('🔄 Повторить обработку', 'retry')])
    else:
        rows.append([button('🔄 Обновить статус', 'open')])
    source = safe_url(item.source_url)
    if source:
        rows.append([InlineKeyboardButton(text='🔗 Исходная публикация', url=source)])
    rows.extend([[button('📄 Присланный текст', 'input')],
                 [button('🗑 Убрать из новостей', 'delete')],
                 [InlineKeyboardButton(text='📥 Новости пользователя', callback_data='news:0')]])
    return with_home(rows)


def ready_text(item):
    title = html.escape(str((item.parsed_data or {}).get('title', 'Новость'))[:160])
    return ('✅ <b>Новость обработана</b>\n' + title +
            '\n\nВыберите действие: следить, сохранить интерес или решить позже. '
            'Карточка сохранена в «Новости пользователя».')
