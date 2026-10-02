"""Small finite vocabulary. Never store user text or arbitrary callback data."""
import hashlib

from sqlalchemy import select

from app.models import AnalyticsState, ProductEvent, User

BUSINESS_EVENTS = {
    'user_started_bot': 'registered', 'story_created': 'watch_started',
    'story_paused': 'watch_paused', 'story_resumed': 'watch_resumed',
    'story_paused_delivery': 'watch_paused_delivery',
    'intensive_enabled': 'intensive_enabled', 'intensive_disabled': 'intensive_disabled',
    'intensive_expired': 'intensive_expired', 'intensive_transferred_from': 'intensive_transferred',
    'interest_saved': 'interest_saved', 'interest_removed': 'interest_removed',
    'manual_check_requested': 'manual_check', 'story_check_completed': 'check_completed',
    'notification_feedback_useful': 'feedback_useful', 'notification_feedback_not_useful': 'feedback_not_useful',
}
CATEGORIES = {'start', 'menu', 'watching', 'news', 'journal', 'interests', 'check', 'feedback',
              'pause', 'resume', 'delete', 'intensive', 'input', 'help', 'other'}
EVENTS = set(BUSINESS_EVENTS.values()) | {f'interaction_{c}' for c in CATEGORIES} | {
    'news_submitted', 'news_ready', 'news_failed', 'news_deferred', 'news_deleted', 'watch_deleted',
    'notification_sent', 'notification_failed', 'ready_notice_sent', 'ready_notice_failed',
    'delivery_forbidden', 'bot_blocked', 'bot_unblocked', 'handler_error'}


def add_event(session, event, user_id, dedupe_key=None):
    if event not in EVENTS:
        raise ValueError('Unknown product event')
    if user_id is not None:
        session.add(ProductEvent(user_id=user_id, event=event, dedupe_key=dedupe_key))


def interaction_category(event):
    """Inspect only a fixed prefix; parameters/invite codes/text never enter analytics."""
    callback = getattr(event, 'data', None)
    if callback is not None:
        prefix = callback.split(':', 1)[0]
        return {
            'home': 'menu', 'main': 'menu', 'watching': 'watching', 'story': 'watching',
            'list': 'watching', 'watch': 'watching', 'card': 'watching', 'history': 'journal',
            'focus': 'intensive', 'interest_view': 'interests', 'interest_remove': 'interests',
            'ndelete': 'delete', 'ndelete_yes': 'delete',
            'news': 'news', 'nopen': 'news', 'nretry': 'news', 'nwatch': 'news', 'nlater': 'news',
            'jp': 'journal', 'ju': 'journal', 'jo': 'journal', 'interests': 'interests',
            'interest': 'interests', 'check': 'check', 'useful': 'feedback', 'not_useful': 'feedback',
            'pause': 'pause', 'resume': 'resume', 'delete': 'delete', 'delete_yes': 'delete',
            'intensive': 'intensive', 'transfer': 'intensive', 'daily': 'intensive',
        }.get(prefix, 'other')
    text = getattr(event, 'text', '') or ''
    if not text.startswith('/') or getattr(event, 'forward_origin', None):
        return 'input'
    command = text.split(maxsplit=1)[0].split('@', 1)[0]
    return {'/start': 'start', '/menu': 'menu', '/news': 'news', '/watching': 'watching',
            '/journal': 'journal', '/interests': 'interests', '/check_now': 'check',
            '/help': 'help', '/cancel': 'other'}.get(command, 'other')


class ProductAnalyticsRepository:
    async def record_product_event(self, event, user_id, dedupe_key=None):
        if event not in EVENTS:
            raise ValueError('Unknown product event')
        key = hashlib.sha256(f'{user_id}:{dedupe_key}'.encode()).hexdigest() if dedupe_key else None
        async with self._transaction() as session:
            # Same lock order as user mutation; serializes redelivery and concurrent polling.
            user = await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update(key_share=True))
            if user is None:
                return False
            if key and await session.scalar(select(ProductEvent.id).where(ProductEvent.dedupe_key == key)):
                return False
            add_event(session, event, user_id, key)
            return True

    async def analytics_started(self):
        async with self._transaction() as session:
            return await session.get(AnalyticsState, 1)
