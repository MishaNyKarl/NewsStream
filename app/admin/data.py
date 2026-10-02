"""Bounded, read-only queries against the existing bot schema. No migrations."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

from sqlalchemy import Integer, String, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Story, StoryUpdate, UsageEvent, User, UserNews, utcnow

MSK = timezone(timedelta(hours=3))


@dataclass
class Filters:
    start: datetime
    end: datetime
    user: int | None
    page: int

    @classmethod
    def parse(cls, query):
        today = datetime.now(MSK).date()
        try:
            start = datetime.strptime(query.get('start', str(today-timedelta(days=6))), '%Y-%m-%d').replace(tzinfo=MSK)
            end = datetime.strptime(query.get('end', str(today)), '%Y-%m-%d').replace(tzinfo=MSK)+timedelta(days=1)
            page = int(query.get('page', '1'))
            user = int(query['user']) if query.get('user') else None
            if not 1 <= (end-start).days <= 366 or not 1 <= page <= 10000:
                raise ValueError
            if user is not None and not 0 < user < 2**63:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError('Период: от 1 до 366 дней; ID пользователя и страница — положительные числа.') from None
        return cls(start.astimezone(timezone.utc), end.astimezone(timezone.utc), user, page)

    def event_conditions(self):
        conditions = [UsageEvent.created_at >= self.start, UsageEvent.created_at < self.end]
        if self.user is not None:
            conditions.append(UsageEvent.user_id == self.user)
        return conditions

    @property
    def form(self):
        return {'start': self.start.astimezone(MSK).date().isoformat(),
                'end': (self.end.astimezone(MSK)-timedelta(days=1)).date().isoformat(),
                'user': self.user or ''}


def safe_detail(value):
    """Allow only telemetry grammar, never arbitrary exception/content strings."""
    if not value:
        return '—'
    if re.fullmatch(r'[a-z_]{1,40}:[a-z_0-9]{1,40}', value):
        return value
    if re.fullmatch(r'[a-z_]{1,40}: [A-Za-z]{1,60}', value):
        return value
    if re.fullmatch(r'sources=\d+; update=(True|False); seconds=\d+(\.\d+)?', value):
        return value
    return 'Подробности скрыты'


def identifier(value):
    # provider/model/operation identifiers, not arbitrary content or URLs.
    if value and (value.startswith(('sk-', 'AIza', 'gsk_')) or re.search(r'\d{5,15}:[A-Za-z0-9_-]{25,}', value)):
        return 'скрыто'
    return value if value and len(value) <= 160 and re.fullmatch(r'[\w./:+-]+', value) else '—'


class Data:
    def __init__(self, url):
        options = {}
        if url.startswith('postgresql'):
            options = {'pool_size': 2, 'max_overflow': 0, 'pool_timeout': 5,
                       'connect_args': {'timeout': 5, 'command_timeout': 8, 'server_settings': {
                           'default_transaction_read_only': 'on', 'statement_timeout': '7000',
                           'application_name': 'newswatch-admin', 'timezone': 'UTC'}}}
        self.engine = create_async_engine(url, echo=False, pool_pre_ping=True, **options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self):
        await self.engine.dispose()

    async def overview(self, filters):
        async with self.sessions() as s:
            await s.execute(text('SELECT 1'))
            def count(model, *where):
                return select(func.count()).select_from(model).where(*where)
            scoped_story = [Story.user_id == filters.user] if filters.user else []
            scoped_news = [UserNews.user_id == filters.user] if filters.user else []
            now = utcnow()
            result = {}
            for label, query in [
                ('Пользователей всего', count(User, *([User.telegram_id == filters.user] if filters.user else []))),
                ('Активны в выбранный период', count(User, User.last_seen_at >= filters.start,
                    User.last_seen_at < filters.end, *([User.telegram_id == filters.user] if filters.user else []))),
                ('Активных наблюдений сейчас', count(Story, Story.status == 'active', *scoped_story)),
                ('На паузе сейчас', count(Story, Story.status == 'paused', *scoped_story)),
                ('Частый режим действует', count(Story, Story.status.in_(('active', 'paused')),
                    Story.monitoring_mode == 'intensive', Story.intensive_until > now, *scoped_story)),
                ('Проверок ждут сейчас', count(Story, Story.status == 'active', Story.next_check_at <= now,
                    or_(Story.lock_until.is_(None), Story.lock_until <= now), *scoped_story)),
                ('Проверок в работе', count(Story, Story.status == 'active', Story.lock_until > now, *scoped_story)),
                ('Новостей в обработке', count(UserNews, UserNews.status.in_(('pending', 'processing')), *scoped_news)),
                ('Ошибок обработки новостей', count(UserNews, UserNews.status == 'failed', *scoped_news)),
                ('Ожидают готовности карточки', count(UserNews, UserNews.status == 'ready',
                    UserNews.notice_suppressed.is_(False), UserNews.notice_sent_at.is_(None), *scoped_news)),
            ]:
                result[label] = await s.scalar(query)
            outbox = select(func.count()).select_from(StoryUpdate).join(Story).where(
                Story.status == 'active', StoryUpdate.notified_at.is_(None), *scoped_story)
            result['Уведомлений ожидает доставки'] = await s.scalar(outbox.where(StoryUpdate.delivery_attempts < 5))
            result['Доставка исчерпала 5 попыток'] = await s.scalar(outbox.where(StoryUpdate.delivery_attempts >= 5))
            result['Ошибок за период'] = await s.scalar(count(UsageEvent, *filters.event_conditions(),
                                                             UsageEvent.operation.contains('error')))
            result['Последняя завершённая проверка'] = await s.scalar(select(func.max(Story.last_checked_at)).where(*scoped_story))
            return result

    async def table(self, name, filters):
        offset = (filters.page-1)*50
        async with self.sessions() as s:
            if name == 'users':
                counts = (select(Story.user_id, func.count().label('count')).where(Story.status == 'active')
                          .group_by(Story.user_id).subquery())
                query = select(User.telegram_id, User.username, User.is_admin, User.created_at,
                               User.last_seen_at, func.coalesce(counts.c.count, 0)).outerjoin(counts,
                               counts.c.user_id == User.telegram_id).order_by(User.last_seen_at.desc(), User.telegram_id)
                query = query.where(User.last_seen_at >= filters.start, User.last_seen_at < filters.end)
                if filters.user:
                    query = query.where(User.telegram_id == filters.user)
                headers = ['Telegram ID', 'Имя', 'Админ бота', 'Создан', 'Последняя активность', 'Активных наблюдений']
            elif name == 'stories':
                query = select(Story.id, Story.user_id, Story.title, Story.status, Story.monitoring_mode,
                               Story.intensive_until, Story.last_checked_at, Story.next_check_at).where(
                               Story.status != 'deleted').order_by(Story.id.desc())
                if filters.user:
                    query = query.where(Story.user_id == filters.user)
                headers = ['ID', 'Пользователь', 'Тема', 'Статус', 'Режим', 'Частый до', 'Проверено', 'Следующая']
            elif name == 'notifications':
                query = select(StoryUpdate.id, Story.user_id, StoryUpdate.story_id, StoryUpdate.update_kind,
                               StoryUpdate.is_demo, StoryUpdate.created_at, StoryUpdate.notified_at,
                               StoryUpdate.delivery_attempts).join(Story).where(
                               StoryUpdate.created_at >= filters.start, StoryUpdate.created_at < filters.end,
                               Story.status != 'deleted').order_by(StoryUpdate.created_at.desc(), StoryUpdate.id.desc())
                if filters.user:
                    query = query.where(Story.user_id == filters.user)
                headers = ['ID', 'Пользователь', 'Тема', 'Тип', 'Демо', 'Создано', 'Доставлено', 'Попыток']
            else:
                query = select(UsageEvent.id, UsageEvent.created_at, UsageEvent.user_id, UsageEvent.story_id,
                               UsageEvent.operation, UsageEvent.detail).where(*filters.event_conditions())
                if name == 'errors':
                    query = query.where(UsageEvent.operation.contains('error'))
                else:
                    query = query.where(UsageEvent.operation.in_(('check', 'story_check_completed', 'story_check_failed')))
                query = query.order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
                headers = ['Событие', 'Время', 'Пользователь', 'Тема', 'Операция', 'Результат / длительность']
            rows = [list(row) for row in (await s.execute(query.limit(51).offset(offset))).all()]
            if name in {'errors', 'checks'}:
                for row in rows:
                    row[4], row[5] = identifier(row[4]), safe_detail(row[5])
            return headers, rows[:50], len(rows) > 50

    async def costs(self, filters, rates):
        where = [*filters.event_conditions(), UsageEvent.operation == 'llm']
        columns = [func.count().label('calls'), func.sum(UsageEvent.estimated_cost).label('recorded'),
                   func.sum(UsageEvent.input_tokens).label('input'), func.sum(UsageEvent.output_tokens).label('output'),
                   func.sum(cast(or_(UsageEvent.input_tokens > 0, UsageEvent.output_tokens > 0),
                                 Integer)).label('with_tokens')]
        async with self.sessions() as s:
            total = dict((await s.execute(select(*columns).where(*where))).mappings().one())
            by_user = [dict(r) for r in (await s.execute(select(UsageEvent.user_id, *columns).where(*where)
                .group_by(UsageEvent.user_id).order_by(func.sum(UsageEvent.estimated_cost).desc(), UsageEvent.user_id)
                .limit(51).offset((filters.page-1)*50))).mappings()]
            day = func.substr(cast(UsageEvent.created_at, String), 1, 10)
            by_day = [dict(r) for r in (await s.execute(select(day.label('day'), *columns).where(*where)
                .group_by(day).order_by(day.desc()))).mappings()]
            requests = [dict(r) for r in (await s.execute(select(UsageEvent.id, UsageEvent.created_at,
                UsageEvent.user_id, UsageEvent.story_id, UsageEvent.provider, UsageEvent.model,
                UsageEvent.input_tokens.label('input'), UsageEvent.output_tokens.label('output'),
                UsageEvent.estimated_cost.label('recorded'), UsageEvent.detail).where(*where)
                .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
                .limit(51).offset((filters.page-1)*50))).mappings()]
            checks = await s.scalar(select(func.count()).select_from(UsageEvent).where(
                *filters.event_conditions(), UsageEvent.operation == 'story_check_completed'))
            for row in [total, *by_user, *by_day, *requests]:
                row['estimate'] = None
                if (row.get('input') or row.get('output')) and all(rates.get(k, '') != '' for k in ('input_rate', 'output_rate')):
                    row['estimate'] = (Decimal(row['input'] or 0)*Decimal(rates['input_rate']) +
                                       Decimal(row['output'] or 0)*Decimal(rates['output_rate'])) / 1_000_000
                if not row.get('calls', 1) or not (row.get('input') or row.get('output') or row.get('recorded')):
                    row['recorded'] = None
                if 'detail' in row:
                    row['detail'] = safe_detail(row['detail'])
                    row['provider'], row['model'] = identifier(row['provider']), identifier(row['model'])
            return {'total': total, 'users': by_user[:50], 'days': by_day, 'requests': requests[:50],
                    'checks': checks, 'more': len(requests) > 50 or len(by_user) > 50}
