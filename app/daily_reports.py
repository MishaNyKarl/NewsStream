"""Daily digest scheduling, snapshots and a globally serial delivery queue."""
import re
from datetime import timedelta
from uuid import uuid4

from aiogram.types import InlineKeyboardButton
from sqlalchemy import or_, select

from app.domain import UserError
from app.journal import MSK
from app.models import DailyReport, Story, StoryUpdate, utcnow
from app.navigation import with_home
from app.product_analytics import add_event

REPORT_PROMPT = ('🕒 Во сколько присылать ежедневный отчёт?\n\n'
                 'Выберите время по Москве или отправьте /report ЧЧ:ММ, например /report 09:30. '
                 'Все обычные подписки будут собраны в одном отчёте. '
                 'Если время совпадёт у нескольких пользователей, отчёты отправятся по очереди.')
QUEUE_LOCK = 419281703


def report_keyboard():
    return with_home([[InlineKeyboardButton(text=value + ' МСК', callback_data='report:' + value)
                       for value in ('09:00', '18:00', '21:00')],
                      [InlineKeyboardButton(text='⚙️ Настройки аккаунта', callback_data='settings:home')]])


def parse_time(value):
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value or ''):
        raise UserError('Укажите время по Москве в формате ЧЧ:ММ, например /report 09:30.')
    hour, minute = map(int, value.split(':'))
    return hour * 60 + minute


def next_report(minute, now):
    planned = now.astimezone(MSK).replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    return planned if planned > now else planned + timedelta(days=1)


class DailyReportRepository:
    async def report_preference(self, user_id):
        async with self._transaction() as session:
            return await session.get(DailyReport, user_id)

    async def set_report_time(self, user_id, value):
        minute = parse_time(value)
        async with self._transaction() as session:
            await self._advisory(session, QUEUE_LOCK)
            pref = await session.get(DailyReport, user_id)
            if pref is None:
                pref = DailyReport(user_id=user_id, since=utcnow() - timedelta(days=1))
                session.add(pref)
            pref.minute = minute
            pref.next_at = next_report(minute, utcnow())
            pref.prompt_sent = True
            return pref

    async def claim_report_prompt(self, user_id=None):
        """Also offers migrated users and expired intensive subscriptions a time choice."""
        async with self._transaction() as session:
            await self._advisory(session, QUEUE_LOCK)
            query = select(Story.user_id).where(Story.status == 'active', Story.monitoring_mode == 'daily')
            if user_id is not None:
                query = query.where(Story.user_id == user_id)
            owners = (await session.scalars(query.distinct().order_by(Story.user_id))).all()
            for owner in owners:
                pref = await session.get(DailyReport, owner)
                if pref is None:
                    pref = DailyReport(user_id=owner, since=utcnow() - timedelta(days=1), prompt_sent=True)
                    session.add(pref)
                    return owner
                if pref.minute is None and not pref.prompt_sent:
                    pref.prompt_sent = True
                    return owner
            return None

    async def reset_report_prompt(self, user_id):
        async with self._transaction() as session:
            pref = await session.get(DailyReport, user_id)
            if pref:
                pref.prompt_sent = False

    async def get_full_update(self, user_id, update_id):
        async with self._transaction() as session:
            row = (await session.execute(select(StoryUpdate, Story).join(Story).where(
                Story.user_id == user_id, Story.status != 'deleted', StoryUpdate.id == update_id))).first()
            return (row[0], row[1]) if row else None

    async def claim_daily_report(self):
        async with self._transaction() as session:
            await self._advisory(session, QUEUE_LOCK)
            now = utcnow()
            # Across worker processes only one report can be sent at a time.
            if await session.scalar(select(DailyReport.user_id).where(DailyReport.locked_until > now).limit(1)):
                return None
            pref = await session.scalar(select(DailyReport).where(
                DailyReport.next_at <= now,
                or_(DailyReport.retry_at.is_(None), DailyReport.retry_at <= now)
            ).order_by(DailyReport.next_at, DailyReport.user_id).limit(1).with_for_update(key_share=True))
            if pref is None:
                return None
            stories = (await session.scalars(select(Story).where(
                Story.user_id == pref.user_id, Story.status == 'active', Story.monitoring_mode == 'daily'
            ).order_by(Story.id))).all()
            if not stories:
                pref.next_at = next_report(pref.minute, now)
                pref.payload = None
                pref.cutoff = None
                pref.sent_parts = 0
                pref.token = None
                pref.locked_until = None
                pref.retry_at = None
                return None
            if pref.payload is None:
                entries = []
                for story in stories:
                    updates = (await session.scalars(select(StoryUpdate).where(
                        StoryUpdate.story_id == story.id, StoryUpdate.created_at > pref.since,
                        StoryUpdate.created_at <= now, StoryUpdate.is_demo.is_(False),
                        StoryUpdate.notified_at.is_(None)
                    ).order_by(StoryUpdate.created_at, StoryUpdate.id))).all()
                    entries.append({'story_id': story.id, 'title': story.title,
                        'updates': [{'id': u.id, 'summary': u.summary, 'kind': u.update_kind} for u in updates],
                        'checked': bool(story.last_checked_at and story.last_checked_at >= pref.since)})
                pref.payload = entries
                pref.cutoff = now
                pref.sent_parts = 0
            pref.token = uuid4().hex
            pref.locked_until = now + timedelta(minutes=5)
            return pref

    async def advance_daily_report(self, user_id, token, sent_parts=None, retry_after=0, forbidden=False,
                                   update_ids=(), message_id=None):
        async with self._transaction() as session:
            await self._advisory(session, QUEUE_LOCK)
            pref = await session.get(DailyReport, user_id)
            if pref is None or pref.token != token or not pref.locked_until or pref.locked_until <= utcnow():
                return False
            if sent_parts is not None:
                for item in (await session.scalars(select(StoryUpdate).join(Story).where(
                    Story.user_id == user_id, StoryUpdate.id.in_(update_ids),
                    StoryUpdate.notified_at.is_(None)))).all():
                    item.notified_at = utcnow()
                    item.telegram_message_id = message_id
                if message_id is not None and sent_parts > pref.sent_parts:
                    self._event(session, 'notification_sent', user_id=user_id)
                    add_event(session, 'notification_sent', user_id)
                pref.sent_parts = sent_parts
                pref.locked_until = utcnow() + timedelta(minutes=5)
                return True
            if forbidden:
                for story in (await session.scalars(select(Story).where(
                    Story.user_id == user_id, Story.status == 'active', Story.monitoring_mode == 'daily'
                ).order_by(Story.id).with_for_update(key_share=True))).all():
                    story.status = 'paused'
                    story.lock_token = story.lock_until = None
                    story.next_check_at = None
                    story.updated_at = utcnow()
                    self._event(session, 'story_paused_delivery', user_id, story.id)
                add_event(session, 'delivery_forbidden', user_id)
            if retry_after:
                pref.retry_at = utcnow() + timedelta(seconds=max(30, retry_after))
                add_event(session, 'notification_failed', user_id)
            else:
                pref.since = pref.cutoff
                pref.next_at = next_report(pref.minute, utcnow())
                pref.payload = None
                pref.cutoff = None
                pref.sent_parts = 0
                pref.retry_at = None
            pref.token = None
            pref.locked_until = None
            return True
