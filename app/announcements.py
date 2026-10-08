"""Shared announcement queue; admin never needs the Telegram token."""
import re
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, text

from app.commerce import Commerce
from app.errors import UserError
from app.models import Announcement, AnnouncementDelivery, DailyReport, Story, User, utcnow

AUDIENCES = {'all': 'Все пользователи', 'daily': 'С обычными подписками',
             'no_time': 'С обычными подписками, ещё без времени отчёта',
             'active': 'С активными наблюдениями', 'selected': 'Выбранные пользователи'}
BUTTONS = {'report': '🕒 Выбрать время отчёта', 'menu': '🏠 Открыть бот', 'none': 'Без кнопки'}
STATUSES = {'draft': 'Черновик', 'queued': 'В очереди', 'completed': 'Завершено', 'cancelled': 'Остановлено',
            'pending': 'Ожидает', 'sending': 'Отправляется', 'sent': 'Доставлено', 'blocked': 'Бот недоступен',
            'failed': 'Ошибка'}
UPDATE_TEMPLATE = dict(title='Новости теперь в ежедневном отчёте', audience='all', button='report', body=(
    'Мы обновили бота. Ваши новости, подписки и история сохранены.\n\n'
    '🗓 Обычные подписки теперь собраны в один ежедневный отчёт. В нём — короткие изменения по каждой новости; '
    'если изменений нет, появится «— Без изменений».\n\n'
    '🕒 Выберите удобное время по Москве кнопкой ниже или командой /report ЧЧ:ММ. '
    'До выбора времени ежедневный отчёт не отправляется. Если время уже выбрано, менять его не нужно.\n\n'
    '⚡ «Следить внимательнее» работает по прежнему расписанию и присылает важные изменения после проверок. '
    'Темы на паузе остаются на паузе.\n\n'
    '📖 «Читать дальше» под новым уведомлением открывает полный отчёт. '
    'В старых сообщениях подробности доступны через историю новости.'))
TEMPLATES = {'update': UPDATE_TEMPLATE,
    'reminder': dict(title='Выберите время ежедневного отчёта', audience='no_time', button='report',
        body='Ваши обычные подписки сохранены. Чтобы получать ежедневный отчёт, выберите удобное время по Москве. '
             'Нажмите кнопку ниже или отправьте /report ЧЧ:ММ. Темы на паузе не включаются до возобновления.'),
    'maintenance': dict(title='Технические работы', audience='all', button='menu',
        body='Укажите время работ по Москве, что будет временно недоступно и когда восстановится работа бота.')}
QUEUE_LOCK = 419281704


def announcement_text(campaign):
    return f'📣 {campaign.title}\n\n{campaign.body}'


class Announcements:
    def __init__(self, sessions):
        self.sessions = sessions

    def transaction(self):
        return Commerce(self.sessions, None).transaction()

    async def lock(self, session):
        if session.bind.dialect.name == 'postgresql':
            await session.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': QUEUE_LOCK})

    async def recipients(self, session, audience, ids=''):
        if audience not in AUDIENCES:
            raise UserError('Выберите аудиторию.')
        query = select(User.telegram_id).order_by(User.telegram_id)
        if audience == 'selected':
            parts = re.split(r'[\s,;]+', ids.strip())
            if not parts or len(parts) > 500 or any(not p.isascii() or not p.isdigit() for p in parts):
                raise UserError('Укажите Telegram ID через запятую или пробел (до 500 пользователей).')
            selected = {int(p) for p in parts}
            if any(not 0 < uid < 2**63 for uid in selected):
                raise UserError('Некорректный Telegram ID.')
            query = query.where(User.telegram_id.in_(selected))
        elif audience != 'all':
            story = select(Story.user_id).where(Story.status.in_(('active', 'paused')))
            if audience in {'daily', 'no_time'}:
                story = story.where(or_(Story.monitoring_mode == 'daily', Story.intensive_until <= utcnow()))
            else:
                story = story.where(Story.status == 'active')
            query = query.where(User.telegram_id.in_(story))
            if audience == 'no_time':
                query = query.outerjoin(DailyReport, DailyReport.user_id == User.telegram_id).where(DailyReport.minute.is_(None))
        owners = list((await session.scalars(query)).all())
        if audience == 'selected' and set(owners) != selected:
            raise UserError('Некоторые ID не найдены: получатель должен сначала открыть бота.')
        return owners

    async def create(self, values, actor, key):
        title, body = values.get('title', '').strip(), values.get('body', '').strip()
        button, audience = values.get('button', 'none'), values.get('audience')
        if not 1 <= len(title) <= 160 or not body or len(body.encode('utf-16-le')) // 2 > 2500:
            raise UserError('Название: 1–160 символов. Текст: до 2500 символов (эмодзи могут занимать два).')
        if any(ord(c) < 32 and c not in '\n\t' for c in title + body) or '\n' in title:
            raise UserError('Название должно быть одной строкой; уберите управляющие символы.')
        if button not in BUTTONS or not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}', key or ''):
            raise UserError('Форма устарела. Откройте оповещения заново.')
        async with self.transaction() as session:
            await self.lock(session)
            existing = await session.scalar(select(Announcement).where(Announcement.key == key))
            if existing:
                return existing
            owners = await self.recipients(session, audience, values.get('ids', ''))
            if not owners:
                raise UserError('В выбранной аудитории нет пользователей.')
            campaign = Announcement(key=key, title=title, body=body, audience=audience, button=button,
                                    created_by=actor[:80], status='draft')
            session.add(campaign)
            await session.flush()
            session.add_all(AnnouncementDelivery(announcement_id=campaign.id, user_id=uid) for uid in owners)
            return campaign

    async def detail(self, campaign_id):
        async with self.transaction() as session:
            campaign = await session.get(Announcement, campaign_id)
            if campaign is None:
                raise UserError('Оповещение не найдено.')
            counts = dict((await session.execute(select(AnnouncementDelivery.status, func.count()).where(
                AnnouncementDelivery.announcement_id == campaign_id).group_by(AnnouncementDelivery.status))).all())
            recipients = (await session.execute(select(AnnouncementDelivery, User, DailyReport.minute)
                .join(User, User.telegram_id == AnnouncementDelivery.user_id)
                .outerjoin(DailyReport, DailyReport.user_id == User.telegram_id)
                .where(AnnouncementDelivery.announcement_id == campaign_id)
                .order_by(AnnouncementDelivery.user_id).limit(100))).all()
            return dict(campaign=campaign, counts=counts, recipients=recipients, total=sum(counts.values()))

    async def dashboard(self):
        async with self.transaction() as session:
            rows = (await session.execute(select(Announcement, AnnouncementDelivery.status, func.count(AnnouncementDelivery.id))
                .outerjoin(AnnouncementDelivery).group_by(Announcement.id, AnnouncementDelivery.status)
                .order_by(Announcement.id.desc()).limit(600))).all()
            campaigns = {}
            for campaign, status, count in rows:
                entry = campaigns.setdefault(campaign.id, dict(campaign=campaign, counts={}, total=0))
                if status:
                    entry['counts'][status] = count
                    entry['total'] += count
            coverage = {key: len(await self.recipients(session, key)) for key in ('all', 'daily', 'no_time')}
            return dict(campaigns=list(campaigns.values())[:100], coverage=coverage)

    async def action(self, campaign_id, action):
        async with self.transaction() as session:
            await self.lock(session)
            campaign = await session.get(Announcement, campaign_id)
            if campaign is None:
                raise UserError('Оповещение не найдено.')
            if action == 'launch':
                if campaign.status == 'draft':
                    campaign.status = 'queued'
                    campaign.launched_at = utcnow()
            elif action == 'cancel':
                if campaign.status in {'draft', 'queued'}:
                    campaign.status = 'cancelled'
                    campaign.finished_at = utcnow()
                    for delivery in (await session.scalars(select(AnnouncementDelivery).where(
                        AnnouncementDelivery.announcement_id == campaign_id, AnnouncementDelivery.status == 'pending'))).all():
                        delivery.status = 'cancelled'
            elif action == 'retry':
                if campaign.status not in {'queued', 'completed'}:
                    raise UserError('Повтор доступен только для запущенной рассылки.')
                failed = (await session.scalars(select(AnnouncementDelivery).where(
                    AnnouncementDelivery.announcement_id == campaign_id, AnnouncementDelivery.status == 'failed'))).all()
                for delivery in failed:
                    delivery.status, delivery.attempts, delivery.error = 'pending', 0, None
                    delivery.retry_at = None
                if failed:
                    campaign.status, campaign.finished_at = 'queued', None
            else:
                raise UserError('Неизвестное действие.')
            return campaign

    async def complete(self, session, campaign):
        remaining = await session.scalar(select(func.count()).select_from(AnnouncementDelivery).where(
            AnnouncementDelivery.announcement_id == campaign.id, AnnouncementDelivery.status.in_(('pending', 'sending'))))
        if not remaining and campaign.status == 'queued':
            campaign.status, campaign.finished_at = 'completed', utcnow()

    async def claim(self):
        async with self.transaction() as session:
            await self.lock(session)
            now = utcnow()
            # A worker may have stopped after cancellation but before acknowledging its lease.
            expired_cancelled = (await session.scalars(select(AnnouncementDelivery).join(Announcement).where(
                Announcement.status == 'cancelled', AnnouncementDelivery.status == 'sending',
                AnnouncementDelivery.locked_until <= now))).all()
            for item in expired_cancelled:
                item.status, item.token, item.locked_until = 'cancelled', None, None
            # Telegram rate limits affect the sender, not just this recipient.
            if await session.scalar(select(AnnouncementDelivery.id).where(
                AnnouncementDelivery.error == 'rate_limit', AnnouncementDelivery.retry_at > now).limit(1)):
                return None
            if await session.scalar(select(AnnouncementDelivery.id).where(
                AnnouncementDelivery.status == 'sending', AnnouncementDelivery.locked_until > now).limit(1)):
                return None
            delivery = await session.scalar(select(AnnouncementDelivery).join(Announcement).where(
                Announcement.status == 'queued', AnnouncementDelivery.status.in_(('pending', 'sending')),
                or_(AnnouncementDelivery.locked_until.is_(None), AnnouncementDelivery.locked_until <= now),
                or_(AnnouncementDelivery.retry_at.is_(None), AnnouncementDelivery.retry_at <= now)
            ).order_by(Announcement.id, AnnouncementDelivery.id).limit(1))
            if delivery is None:
                for campaign in (await session.scalars(select(Announcement).where(Announcement.status == 'queued'))).all():
                    await self.complete(session, campaign)
                return None
            campaign = await session.get(Announcement, delivery.announcement_id)
            if delivery.attempts >= 5:
                delivery.status, delivery.error = 'failed', 'lease_expired'
                await self.complete(session, campaign)
                return None
            delivery.status = 'sending'
            delivery.attempts += 1
            delivery.token = uuid4().hex
            delivery.locked_until = now + timedelta(minutes=2)
            return delivery, campaign

    async def may_send(self, delivery_id, token):
        async with self.transaction() as session:
            return bool(await session.scalar(select(AnnouncementDelivery.id).join(Announcement).where(
                AnnouncementDelivery.id == delivery_id, AnnouncementDelivery.token == token,
                AnnouncementDelivery.status == 'sending', AnnouncementDelivery.locked_until > utcnow(),
                Announcement.status == 'queued')))

    async def finish(self, delivery_id, token, outcome, message_id=None, error=None, retry_after=0):
        async with self.transaction() as session:
            await self.lock(session)
            delivery = await session.get(AnnouncementDelivery, delivery_id)
            if delivery is None or delivery.token != token or delivery.status != 'sending' or delivery.locked_until <= utcnow():
                return False
            campaign = await session.get(Announcement, delivery.announcement_id)
            delivery.token, delivery.locked_until = None, None
            delivery.error = error
            if error == 'rate_limit' and retry_after:
                delivery.retry_at = utcnow() + timedelta(seconds=retry_after)
            if outcome == 'sent':
                delivery.status, delivery.sent_at, delivery.message_id = 'sent', utcnow(), message_id
            elif outcome == 'blocked':
                delivery.status = 'blocked'
            elif outcome == 'permanent':
                delivery.status = 'failed'
            elif campaign.status == 'cancelled' or outcome == 'cancelled':
                delivery.status = 'cancelled'
            elif delivery.attempts >= 5:
                delivery.status = 'failed'
            else:
                delivery.status = 'pending'
                delivery.retry_at = utcnow() + timedelta(seconds=max(retry_after, min(3600, 30 * 2**delivery.attempts)))
            await self.complete(session, campaign)
            return True
