"""Product metrics from explicit events. No inference of screen time, opens or reads."""
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import case, func, select

from app.admin.data import MSK
from app.models import Account, AnalyticsState, ProductEvent, UsageEvent, User, utcnow

SESSION_GAP = timedelta(minutes=30)
MAX_EVENTS = 25_000
LABELS = {
    'news_submitted': 'Прислали новость', 'news_ready': 'Получили готовую карточку',
    'news_failed': 'Ошибка обработки новости', 'news_deferred': 'Отложили решение',
    'news_deleted': 'Удалили сохранённую новость', 'watch_started': 'Включили наблюдение',
    'watch_paused': 'Поставили наблюдение на паузу', 'watch_resumed': 'Возобновили наблюдение',
    'watch_deleted': 'Удалили наблюдение', 'watch_paused_delivery': 'Автопауза после запрета доставки',
    'intensive_enabled': 'Включили частый режим', 'intensive_disabled': 'Отключили частый режим',
    'intensive_expired': 'Частый режим закончился', 'manual_check': 'Запустили ручную проверку',
    'check_completed': 'Завершена проверка', 'notification_sent': 'Доставлено развитие',
    'notification_failed': 'Неудачная попытка доставки развития', 'ready_notice_sent': 'Доставлена готовность карточки',
    'ready_notice_failed': 'Неудачная доставка готовности', 'delivery_forbidden': 'Telegram запретил доставку',
    'bot_blocked': 'Заблокировали бота', 'bot_unblocked': 'Разблокировали бота',
    'feedback_useful': 'Оценили как полезное', 'feedback_not_useful': 'Оценили как неважное',
    'interest_saved': 'Сохранили интерес', 'interest_removed': 'Удалили интерес',
}


def day(value):
    return value.astimezone(MSK).date()


def seconds_median(values):
    return round(median(values), 1) if values else None


def analyse(users, events, start, end, coverage, now=None):
    """Pure, testable metrics. Input events include a 30-day lookback for interval context."""
    cutoff = min(end, now or utcnow())
    if cutoff <= start:
        raise ValueError('Выберите период, который уже начался.')
    anchor = day(cutoff-timedelta(microseconds=1))
    observed_start = max(coverage, datetime.combine(anchor-timedelta(days=29), time.min, MSK))
    events = sorted((e for e in events if e['user_id'] in users and e['created_at'] < cutoff),
                    key=lambda e: (e['created_at'], e['id']))
    interactions = defaultdict(list)
    selected = defaultdict(list)
    counts, adopters = Counter(), defaultdict(set)
    daily_users, daily_actions = defaultdict(set), Counter()
    all_by_user = defaultdict(list)
    for event in events:
        uid, at, kind = event['user_id'], event['created_at'], event['event']
        all_by_user[uid].append(event)
        if kind.startswith('interaction_'):
            interactions[uid].append(at)
            daily_users[day(at)].add(uid)
            daily_actions[day(at)] += 1
            if start <= at:
                selected[uid].append(at)
        if start <= at:
            counts[kind] += 1
            adopters[kind].add(uid)
    rows, sessions_all, spans, gaps_all = [], [], [], []
    for uid, user in users.items():
        if user['created_at'] >= cutoff:
            continue
        visits = []
        for at in interactions[uid]:
            if not visits or at-visits[-1][-1] >= SESSION_GAP:
                visits.append([at])
            else:
                visits[-1].append(at)
        sessions = [v for v in visits if v[-1] >= start]
        gaps = [(v[0]-visits[i-1][-1]).total_seconds() for i, v in enumerate(visits) if i and v[0] >= start]
        gaps_all.extend(gaps)
        session_spans = []
        for v in sessions:
            visible = [at for at in v if at >= start]
            # A single action provides no evidence of how long the user stayed.
            span = (visible[-1]-visible[0]).total_seconds() if len(visible) > 1 else None
            if span is not None:
                spans.append(span)
                session_spans.append(span)
            sessions_all.append({'user_id': uid, 'start': visible[0], 'end': visible[-1],
                                 'actions': len(visible), 'span_seconds': span,
                                 'boundary': v[0] < start or cutoff-v[-1] < SESSION_GAP})
        last = interactions[uid][-1] if interactions[uid] else None
        membership = [e for e in all_by_user[uid] if e['event'] in {'bot_blocked', 'bot_unblocked'}]
        rows.append({'user_id': uid, 'admin': user['is_admin'], 'created_at': user['created_at'],
                     'actions': len(selected[uid]), 'active_days': len({day(at) for at in selected[uid]}),
                     'sessions': len(sessions), 'median_span': seconds_median(session_spans),
                     'median_gap': seconds_median(gaps), 'last_action': last,
                     'inactive_days': (cutoff-last).total_seconds()/86400 if last else None,
                     'block_state': membership[-1]['event'] if membership else 'unknown'})
    active = {uid for uid, times in selected.items() if times}
    def active_window(days):
        since = anchor-timedelta(days=days-1)
        return len(set().union(*(uids for d, uids in daily_users.items() if since <= d <= anchor)))
    dau, wau, mau = (active_window(n) for n in (1, 7, 30))
    available_days = max(1, (anchor-day(observed_start)).days+1)
    mean_dau = sum(len(u) for d, u in daily_users.items() if day(observed_start) <= d <= anchor)/available_days
    # Cohorts use registration, never first observed action of an old account.
    cohort = {uid: u for uid, u in users.items() if max(start, coverage) <= u['created_at'] < cutoff}
    retention = []
    for n in (1, 7, 30):
        eligible, returned = 0, 0
        for uid, u in cohort.items():
            target = day(u['created_at'])+timedelta(days=n)
            matured = datetime.combine(target+timedelta(days=1), time.min, MSK) <= cutoff
            if matured:
                eligible += 1
                returned += any(day(at) == target for at in interactions[uid])
        retention.append({'day': n, 'eligible': eligible, 'returned': returned,
                          'percent': 100*returned/eligible if eligible else None})
    funnel_names = [('registered', 'Регистрация'), ('news_submitted', 'Новость прислана'),
                    ('news_ready', 'Карточка готова'), ('watch_started', 'Наблюдение включено'),
                    ('notification_sent', 'Развитие доставлено'), ('feedback_useful', 'Полезность подтверждена')]
    reached = {uid: u['created_at'] for uid, u in cohort.items()}
    funnel = [{'label': funnel_names[0][1], 'users': len(reached), 'percent': 100.0 if reached else None}]
    time_to_activation = []
    for event_name, label in funnel_names[1:]:
        next_reached = {}
        for uid, previous in reached.items():
            found = next((e['created_at'] for e in all_by_user[uid]
                          if e['event'] == event_name and e['created_at'] >= previous), None)
            if found:
                next_reached[uid] = found
                if event_name == 'watch_started':
                    time_to_activation.append((found-cohort[uid]['created_at']).total_seconds())
        reached = next_reached
        funnel.append({'label': label, 'users': len(reached), 'percent': 100*len(reached)/len(cohort) if cohort else None})
    chart = []
    cursor = day(start)
    while cursor <= anchor:
        available = datetime.combine(cursor+timedelta(days=1), time.min, MSK) > coverage
        chart.append({'day': str(cursor), 'users': len(daily_users[cursor]) if available else None,
                      'actions': daily_actions[cursor] if available else None})
        cursor += timedelta(days=1)
    features = [{'event': k, 'label': label, 'count': counts[k], 'users': len(adopters[k])} for k, label in LABELS.items()]
    return {'coverage': coverage, 'cutoff': cutoff, 'anchor': anchor, 'users_total': len(rows),
            'active': len(active), 'new_users': len(cohort), 'actions': sum(len(t) for t in selected.values()),
            'dau': dau, 'wau': wau, 'mau': mau, 'observed_days': available_days,
            'stickiness': 100*mean_dau/mau if mau else None,
            'sessions_count': len(sessions_all), 'single_sessions': sum(v['actions'] == 1 for v in sessions_all),
            'median_span': seconds_median(spans), 'median_gap': seconds_median(gaps_all),
            'activation_seconds': seconds_median(time_to_activation), 'retention': retention, 'funnel': funnel,
            'features': features, 'daily': chart, 'users': sorted(rows, key=lambda r: (-r['actions'], r['user_id'])),
            'sessions': sorted(sessions_all, key=lambda r: r['start'], reverse=True),
            'notifications': counts['notification_sent'], 'pausers': len(adopters['watch_paused']),
            'blockers': len(adopters['bot_blocked']),
            'feedback_useful': counts['feedback_useful'], 'feedback_not_useful': counts['feedback_not_useful']}


def billing(rows):
    totals = {'calls': 0, 'confirmed': None, 'estimated': None, 'legacy': None,
              'confirmed_calls': 0, 'estimated_calls': 0, 'unknown_calls': 0}
    for row in rows:
        totals['calls'] += 1
        if row['cost_source'] == 'provider' and row['currency'] == 'USD' and row['actual_cost'] is not None:
            key, value, counter = 'confirmed', row['actual_cost'], 'confirmed_calls'
        elif row['cost_source'] == 'estimate' and row['currency'] == 'USD':
            key, value, counter = 'estimated', row['estimated_cost'], 'estimated_calls'
        else:
            key, value, counter = 'legacy', row['estimated_cost'], 'unknown_calls'
        totals[counter] += 1
        # Unknown zero is missing information, not a free request.
        if value is not None and (key != 'legacy' or value > 0):
            totals[key] = (totals[key] or Decimal(0))+Decimal(str(value))
    return totals


async def report(data, filters, include_admins=False):
    now = utcnow()
    cutoff = min(filters.end, now)
    async with data.sessions() as session:
        coverage = await session.scalar(select(AnalyticsState.started_at).where(AnalyticsState.id == 1))
        if coverage is None:
            return {'unavailable': True}
        admin_flag = case((Account.role == 'admin', True), (Account.role == 'user', False), else_=User.is_admin)
        uq = select(User.telegram_id, User.created_at, admin_flag.label('is_admin')).outerjoin(
            Account, Account.user_id == User.telegram_id).where(User.created_at < cutoff)
        if not include_admins:
            uq = uq.where(admin_flag.is_(False))
        if filters.user:
            uq = uq.where(User.telegram_id == filters.user)
        user_rows = (await session.execute(uq.limit(10001))).mappings().all()
        if len(user_rows) > 10000:
            raise ValueError('Слишком много пользователей. Выберите Telegram ID.')
        users = {r['telegram_id']: dict(r) for r in user_rows}
        # Keep queries bounded, but reject rather than publish silently truncated metrics.
        since = filters.start-timedelta(days=30)
        query = select(ProductEvent.id, ProductEvent.user_id, ProductEvent.event, ProductEvent.created_at).join(
            User, User.telegram_id == ProductEvent.user_id).outerjoin(Account, Account.user_id == User.telegram_id).where(ProductEvent.created_at >= since,
            ProductEvent.created_at < cutoff)
        costs_query = select(UsageEvent.user_id, UsageEvent.request_id, UsageEvent.created_at,
            UsageEvent.cost_source, UsageEvent.currency, UsageEvent.actual_cost, UsageEvent.estimated_cost,
            UsageEvent.provider, UsageEvent.model).join(User, User.telegram_id == UsageEvent.user_id).outerjoin(Account, Account.user_id == User.telegram_id).where(
            UsageEvent.operation == 'llm', UsageEvent.created_at >= filters.start, UsageEvent.created_at < cutoff)
        if not include_admins:
            query = query.where(admin_flag.is_(False))
            costs_query = costs_query.where(admin_flag.is_(False))
        if filters.user:
            query = query.where(User.telegram_id == filters.user)
            costs_query = costs_query.where(UsageEvent.user_id == filters.user)
        events = [dict(r) for r in (await session.execute(query.order_by(ProductEvent.created_at, ProductEvent.id)
                                                        .limit(MAX_EVENTS+1))).mappings()]
        costs = [dict(r) for r in (await session.execute(costs_query.limit(MAX_EVENTS+1))).mappings()]
        if len(events) > MAX_EVENTS or len(costs) > MAX_EVENTS:
            raise ValueError('Более 25 000 событий в выборке. Сократите период или выберите пользователя.')
        # Unknown/unassigned costs are not attributed to a random user or excluded silently.
        unattributed = await session.scalar(select(func.count()).select_from(UsageEvent).where(
            UsageEvent.operation == 'llm', UsageEvent.user_id.is_(None), UsageEvent.created_at >= filters.start,
            UsageEvent.created_at < cutoff))
    result = analyse(users, events, filters.start, filters.end, coverage, now)
    result['billing'] = billing(costs)
    result['unattributed_calls'] = unattributed
    result['billing']['per_active_confirmed'] = (result['billing']['confirmed']/result['active']
        if result['billing']['confirmed'] is not None and result['active'] else None)
    result['billing']['per_notification_confirmed'] = (result['billing']['confirmed']/result['notifications']
        if result['billing']['confirmed'] is not None and result['notifications'] else None)
    by_user, by_request = defaultdict(list), defaultdict(list)
    for row in costs:
        by_user[row['user_id']].append(row)
        if row['request_id']:
            by_request[(row['user_id'], row['request_id'])].append(row)
    for row in result['users']:
        row['billing'] = billing(by_user[row['user_id']])
    result['requests'] = sorted([{'user_id': uid, 'id': rid, 'at': max(r['created_at'] for r in rows),
                                  **billing(rows)} for (uid, rid), rows in by_request.items()],
                                 key=lambda r: r['at'], reverse=True)
    result['requestless_calls'] = sum(r['request_id'] is None for r in costs)
    return result


def cost_columns():
    """SQL aggregates shared with the existing cost page."""
    verified = (UsageEvent.cost_source == 'provider') & (UsageEvent.currency == 'USD') & UsageEvent.actual_cost.is_not(None)
    return [func.sum(case((verified, UsageEvent.actual_cost), else_=None)).label('confirmed')]
