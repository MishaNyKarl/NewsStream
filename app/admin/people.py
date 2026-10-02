from sqlalchemy import String, case, cast, func, or_, select

from app.models import Account, Plan, ProductEvent, Story, UsageEvent, User, UserNews, UserProfile

INTERACTIONS = {'start': 'Запустил бота', 'menu': 'Открыл меню', 'watching': 'Открыл наблюдения',
    'news': 'Действие с новостью', 'journal': 'Открыл журнал', 'interests': 'Действие с интересами',
    'check': 'Запросил проверку', 'feedback': 'Нажал оценку', 'pause': 'Нажал паузу',
    'resume': 'Нажал возобновление', 'delete': 'Запросил удаление', 'intensive': 'Действие со срочным режимом',
    'input': 'Прислал сообщение', 'help': 'Открыл помощь', 'account': 'Открыл подписку и кредиты', 'other': 'Другое действие'}


def event_name(value):
    from app.admin.product import LABELS
    if value.startswith('interaction_'):
        return INTERACTIONS.get(value.removeprefix('interaction_'), 'Действие в боте')
    return LABELS.get(value, {'registered': 'Зарегистрировался', 'intensive_transferred': 'Перенёс срочный слот'}.get(value, value))


async def directory(data, filters, search=''):
    async with data.sessions() as session:
        activity = select(ProductEvent.user_id,
            func.sum(case((ProductEvent.event.startswith('interaction_'), 1), else_=0)).label('actions'),
            func.sum(case((ProductEvent.event == 'news_submitted', 1), else_=0)).label('submitted'),
            func.sum(case((ProductEvent.event == 'check_completed', 1), else_=0)).label('checks'),
            func.sum(case((ProductEvent.event == 'notification_sent', 1), else_=0)).label('notifications')
        ).where(ProductEvent.created_at >= filters.start, ProductEvent.created_at < filters.end
        ).group_by(ProductEvent.user_id).subquery()
        stories = select(Story.user_id,
            func.sum(case((Story.status == 'active', 1), else_=0)).label('active'),
            func.sum(case((Story.status == 'paused', 1), else_=0)).label('paused')
        ).where(Story.status != 'deleted').group_by(Story.user_id).subquery()
        costs = select(UsageEvent.user_id, func.count().label('llm_calls'),
            func.sum(case(((UsageEvent.cost_source == 'provider') & (UsageEvent.currency == 'USD'),
                          UsageEvent.actual_cost), else_=None)).label('usd')
        ).where(UsageEvent.operation == 'llm', UsageEvent.created_at >= filters.start,
                UsageEvent.created_at < filters.end).group_by(UsageEvent.user_id).subquery()
        query = select(User.telegram_id, func.coalesce(UserProfile.display_name, User.first_name).label('name'),
            func.coalesce(UserProfile.username, User.username).label('username'),
            UserProfile.avatar.is_not(None).label('avatar'), User.created_at, User.last_seen_at,
            case((Account.role == 'admin', True), (Account.role == 'user', False), else_=User.is_admin).label('admin'),
            Account.balance, Account.plan_id, Account.expires_at, Plan.name.label('plan'),
            activity.c.actions, activity.c.submitted, activity.c.checks, activity.c.notifications,
            stories.c.active, stories.c.paused, costs.c.llm_calls, costs.c.usd
        ).outerjoin(UserProfile, UserProfile.user_id == User.telegram_id
        ).outerjoin(Account, Account.user_id == User.telegram_id).outerjoin(Plan, Plan.id == Account.plan_id
        ).outerjoin(activity, activity.c.user_id == User.telegram_id
        ).outerjoin(stories, stories.c.user_id == User.telegram_id).outerjoin(costs, costs.c.user_id == User.telegram_id)
        if filters.user:
            query = query.where(User.telegram_id == filters.user)
        if search:
            term = search.lstrip('@')
            query = query.where(or_(User.first_name.icontains(term, autoescape=True),
                User.username.icontains(term, autoescape=True), UserProfile.display_name.icontains(term, autoescape=True),
                UserProfile.username.icontains(term, autoescape=True), cast(User.telegram_id, String) == term))
        rows = [dict(r) for r in (await session.execute(query.order_by(User.last_seen_at.desc(), User.telegram_id)
            .offset((filters.page-1)*50).limit(51))).mappings()]
        result = dict(people=rows[:50], more=len(rows)>50, events=[], stories=[], news=[])
        if filters.user and rows:
            result['events'] = list(await session.scalars(select(ProductEvent).where(
                ProductEvent.user_id == filters.user, ProductEvent.created_at >= filters.start,
                ProductEvent.created_at < filters.end).order_by(ProductEvent.created_at.desc(), ProductEvent.id.desc()).limit(50)))
            result['stories'] = list(await session.scalars(select(Story).where(Story.user_id == filters.user,
                Story.status != 'deleted').order_by(Story.updated_at.desc()).limit(25)))
            news = (await session.execute(select(UserNews.id, UserNews.status, UserNews.created_at, UserNews.parsed_data)
                .where(UserNews.user_id == filters.user).order_by(UserNews.id.desc()).limit(25))).mappings()
            result['news'] = [dict(id=n['id'], status=n['status'], created_at=n['created_at'],
                                   title=(n['parsed_data'] or {}).get('title', 'Ещё не разобрана')) for n in news]
        return result
