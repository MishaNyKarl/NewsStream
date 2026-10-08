"""Transactional tariffs, entitlements and credit ledger. No payment gateway."""
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4
from weakref import WeakKeyDictionary

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.errors import UserError
from app.models import Account, Charge, CommerceAudit, CreditEntry, Plan, TopUp, UsageEvent, User, UserProfile, utcnow


_sqlite_locks = WeakKeyDictionary()


async def limits(session, uid, settings):
    account = await session.get(Account, uid)
    plan = await session.get(Plan, account.plan_id) if account and account.plan_id and (
        account.expires_at is None or account.expires_at > utcnow()) else None
    result = dict(stories=settings.max_stories_per_user, manual_daily=settings.max_manual_checks_per_day,
                  llm_daily=settings.llm_daily_call_limit, intensive_slots=1, discussion=False, full_reports=False,
                  news_credits=0, check_credits=0, discussion_credits=0, plan_id=None, name='Базовый тест')
    if plan:
        result.update({k: getattr(plan, k) for k in result if k not in {'plan_id'}})
        result['plan_id'] = plan.id
    if account:
        for field, target in [('stories_override', 'stories'), ('manual_override', 'manual_daily'),
                              ('llm_override', 'llm_daily'), ('intensive_override', 'intensive_slots'),
                              ('discussion_override', 'discussion')]:
            if getattr(account, field) is not None:
                result[target] = getattr(account, field)
        if account.credit_exempt:
            for field in ('news_credits', 'check_credits', 'discussion_credits'):
                result[field] = 0
    return result


class Commerce:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings

    @asynccontextmanager
    async def transaction(self):
        # SQLite only in tests. Production row locks serialize all balance edits.
        import asyncio
        async with self.sessions() as session:
            session.sync_session.expire_on_commit = False
            lock = _sqlite_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock()) if session.bind.dialect.name == 'sqlite' else None
            if lock:
                await lock.acquire()
            try:
                async with session.begin():
                    yield session
            finally:
                if lock:
                    lock.release()

    async def account(self, session, uid):
        if not await session.get(User, uid):
            raise UserError('Пользователь не найден. Сначала он должен открыть бота.')
        insert = pg_insert if session.bind.dialect.name == 'postgresql' else sqlite_insert
        await session.execute(insert(Account).values(user_id=uid, balance=0, role='inherit', credit_exempt=False,
                                                     version=0).on_conflict_do_nothing(index_elements=['user_id']))
        return await session.scalar(select(Account).where(Account.user_id == uid).with_for_update(key_share=True))

    async def snapshot(self, uid):
        async with self.sessions() as session:
            account = await session.get(Account, uid)
            return {'account': account, 'limits': await limits(session, uid, self.settings)}

    async def credit_history(self, uid):
        async with self.sessions() as session:
            return list(await session.scalars(select(CreditEntry).where(
                CreditEntry.user_id == uid, CreditEntry.delta != 0).order_by(
                CreditEntry.created_at.desc(), CreditEntry.id.desc()).limit(10)))

    async def catalog(self):
        async with self.sessions() as session:
            return list(await session.scalars(select(Plan).order_by(Plan.price_minor, Plan.id).limit(50)))

    async def set_promotions(self, uid, enabled):
        async with self.transaction() as session:
            account = await self.account(session, uid)
            account.promotions_enabled = bool(enabled)
            account.version += 1

    async def admin_self_plan(self, uid, plan_id, key):
        """Sandbox entitlement assignment, never a paid order or balance adjustment."""
        async with self.transaction() as session:
            if session.bind.dialect.name == 'postgresql':
                from sqlalchemy import text
                await session.execute(text('SELECT pg_advisory_xact_lock(419281703)'))
            account = await self.account(session, uid)
            user = await session.get(User, uid)
            inherited = user.is_admin or uid in self.settings.admin_ids
            if not (inherited if account.role == 'inherit' else account.role == 'admin'):
                raise UserError('Это действие доступно только администратору.')
            if await session.scalar(select(CommerceAudit.id).where(CommerceAudit.key == key)):
                return
            plan = await session.get(Plan, plan_id) if plan_id else None
            if plan_id and plan is None:
                raise UserError('Тариф больше недоступен. Откройте каталог заново.')
            before = account.plan_id
            account.plan_id = plan.id if plan else None
            account.expires_at = utcnow() + timedelta(days=plan.period_days) if plan else None
            account.version += 1
            session.add(CommerceAudit(key=key, actor=f'telegram:{uid}', user_id=uid,
                action='admin_self_plan', detail={'before': before, 'plan_id': account.plan_id,
                    'reason': 'Тестовое подключение администратором; без оплаты'}))

    async def role(self, uid, inherited):
        async with self.sessions() as session:
            account = await session.get(Account, uid)
            return inherited if not account or account.role == 'inherit' else account.role == 'admin'

    @staticmethod
    def entry(session, account, delta, kind, key, actor, reason):
        balance = account.balance + delta
        if not 0 <= balance <= 1_000_000_000:
            raise UserError('Недостаточно кредитов или превышен предел баланса.')
        account.balance = balance
        account.version += 1
        session.add(CreditEntry(user_id=account.user_id, key=key, delta=delta, balance_after=balance,
                                kind=kind, actor=actor, reason=reason))

    async def reserve(self, uid, operation, key):
        if operation not in {'news', 'check', 'discussion'}:
            raise ValueError('Invalid operation')
        async with self.transaction() as session:
            account = await self.account(session, uid)
            prior = await session.get(Charge, key)
            if prior:
                raise UserError('Операция уже учтена. Повторное списание запрещено.')
            policy = await limits(session, uid, self.settings)
            if operation == 'discussion' and not policy['discussion']:
                raise UserError('Обсуждение недоступно в вашем тарифе. Подробнее: /account')
            amount = policy[f'{operation}_credits']
            if amount > account.balance:
                raise UserError('Недостаточно кредитов. Баланс и тариф: /account. Обратитесь к администратору для пополнения.')
            self.entry(session, account, -amount, 'reserve', key+':reserve', 'system', operation)
            session.add(Charge(id=key, user_id=uid, plan_id=policy['plan_id'], operation=operation,
                               amount=amount, status='reserved'))
        return key

    async def settle(self, uid, key, success):
        async with self.transaction() as session:
            account = await self.account(session, uid)
            charge = await session.get(Charge, key)
            if not charge or charge.user_id != uid or charge.status != 'reserved':
                return
            charge.status = 'completed' if success else 'refunded'
            charge.finished_at = utcnow()
            if not success:
                self.entry(session, account, charge.amount, 'refund', key+':refund', 'system', charge.operation)

    async def command(self, action, payload, actor, key):
        """Owner-only web commands. Validation + audit and mutation are one transaction."""
        if not actor or len(actor) > 80 or len(key) != 36:
            raise UserError('Некорректный автор или ключ операции.')
        async with self.transaction() as session:
            # Serialize duplicate form submissions, including creation of plans.
            if session.bind.dialect.name == 'postgresql':
                from sqlalchemy import text
                await session.execute(text('SELECT pg_advisory_xact_lock(419281703)'))
            if await session.scalar(select(CommerceAudit.id).where(CommerceAudit.key == key)):
                return
            def number(name, low=0, high=1_000_000, optional=False):
                raw = payload.get(name, '')
                if optional and raw == '':
                    return None
                try:
                    value = int(raw)
                except (ValueError, TypeError):
                    raise UserError(f'Поле {name}: требуется целое число.') from None
                if not low <= value <= high:
                    raise UserError(f'Поле {name}: допустимо от {low} до {high}.')
                return value
            reason = payload.get('reason', '').strip()
            if not 3 <= len(reason) <= 240:
                raise UserError('Укажите причину от 3 до 240 символов.')
            uid = None if action in {'plan', 'plan_features'} else number('user_id', 1, 2**63-1)
            account = await self.account(session, uid) if uid else None
            detail = {'reason': reason}
            if action == 'plan':
                name, experiment = payload.get('name', '').strip(), payload.get('experiment', '').strip()
                if not 1 <= len(name) <= 80 or len(experiment) > 80:
                    raise UserError('Название и эксперимент: максимум 80 символов.')
                values = {k: number(k, 0, maximum) for k, maximum in {
                    'stories': 1000, 'manual_daily': 1000, 'llm_daily': 10000, 'intensive_slots': 100,
                    'news_credits': 100000, 'check_credits': 100000, 'discussion_credits': 100000,
                    'price_minor': 100000000}.items()}
                plan = Plan(name=name, experiment=experiment, currency='RUB',
                            discussion=payload.get('discussion') == '1', full_reports=payload.get('full_reports') == '1',
                            period_days=number('period_days', 1, 3650), **values)
                session.add(plan)
                await session.flush()
                detail.update(plan_id=plan.id, full_reports=plan.full_reports, **values)
            elif action == 'plan_features':
                plan = await session.scalar(select(Plan).where(Plan.id == number('plan_id', 1))
                    .with_for_update(key_share=True))
                if plan is None:
                    raise UserError('Тариф не найден.')
                if payload.get('full_reports') not in {'0', '1'}:
                    raise UserError('Выберите доступ к полному отчёту.')
                detail.update(plan_id=plan.id, before=plan.full_reports)
                plan.full_reports = payload['full_reports'] == '1'
                detail['full_reports'] = plan.full_reports
            elif action == 'account':
                if account.version != number('version'):
                    raise UserError('Баланс или настройки изменились. Обновите страницу и повторите.')
                plan_id = number('plan_id', 1, optional=True)
                chosen = await session.get(Plan, plan_id) if plan_id else None
                if plan_id and not chosen:
                    raise UserError('Тариф не найден.')
                days = number('days', 1, 3650, optional=True)
                role = payload.get('role')
                if role not in {'inherit', 'user', 'admin'}:
                    raise UserError('Некорректная роль.')
                detail['before'] = {k: getattr(account, k) for k in ('plan_id', 'role', 'credit_exempt')}
                if plan_id != account.plan_id or days is not None:
                    account.expires_at = utcnow()+timedelta(days=days or chosen.period_days) if chosen else None
                account.plan_id, account.role = plan_id, role
                account.credit_exempt = payload.get('credit_exempt') == '1'
                for field, maximum in [('stories_override', 1000), ('manual_override', 1000),
                                       ('llm_override', 10000), ('intensive_override', 100)]:
                    setattr(account, field, number(field, 0, maximum, optional=True))
                choice = payload.get('discussion_override', '')
                if choice not in {'', '0', '1'}:
                    raise UserError('Некорректная привилегия обсуждения.')
                account.discussion_override = None if choice == '' else choice == '1'
                account.version += 1
                detail.update(plan_id=plan_id, role=role, days=days, credit_exempt=account.credit_exempt,
                    expires_at=account.expires_at.isoformat() if account.expires_at else None,
                    overrides={k: getattr(account, k) for k in ('stories_override', 'manual_override',
                        'llm_override', 'intensive_override', 'discussion_override')})
            elif action in {'grant', 'set_balance'}:
                value = number('amount', -1_000_000 if action == 'grant' else 0, 1_000_000_000)
                if action == 'set_balance' and account.version != number('version'):
                    raise UserError('Баланс изменился. Обновите страницу.')
                delta = value if action == 'grant' else value-account.balance
                held = await session.scalar(select(func.coalesce(func.sum(Charge.amount), 0)).where(
                    Charge.user_id == uid, Charge.status == 'reserved'))
                if account.balance + delta + held > 1_000_000_000:
                    raise UserError('Баланс с незавершёнными резервами превышает предел.')
                self.entry(session, account, delta, action, key, actor, reason)
                detail.update(delta=delta, balance_after=account.balance)
            elif action == 'topup':
                policy = await limits(session, uid, self.settings)
                session.add(TopUp(id=key, user_id=uid, plan_id=policy['plan_id'], credits=number('credits', 1),
                                 amount_minor=number('amount_minor', 1, 100000000), currency='RUB'))
                detail['order'] = key
            elif action in {'paid', 'cancel'}:
                order = await session.get(TopUp, payload.get('order', ''))
                if not order or order.user_id != uid or order.status != 'pending':
                    raise UserError('Заявка не найдена или уже обработана.')
                if action == 'paid':
                    reference = payload.get('reference', '').strip()
                    if not 3 <= len(reference) <= 120:
                        raise UserError('Нужен уникальный номер подтверждённого платежа (3–120 символов).')
                    if await session.scalar(select(TopUp.id).where(TopUp.reference == reference)):
                        raise UserError('Этот платёж уже использован.')
                    held = await session.scalar(select(func.coalesce(func.sum(Charge.amount), 0)).where(
                        Charge.user_id == uid, Charge.status == 'reserved'))
                    if account.balance + order.credits + held > 1_000_000_000:
                        raise UserError('Баланс с резервами превышает предел.')
                    order.reference, order.paid_at = reference, utcnow()
                    order.status = 'manual_paid'
                    self.entry(session, account, order.credits, 'topup', 'topup:'+order.id, actor, reason)
                else:
                    order.status = 'cancelled'
                detail.update(order=order.id, status=order.status)
            elif action == 'refund_stale':
                charge = await session.get(Charge, payload.get('charge', ''))
                if not charge or charge.user_id != uid or charge.status != 'reserved' or charge.created_at > utcnow()-timedelta(minutes=30):
                    raise UserError('Возврат доступен только для зависшего резерва старше 30 минут.')
                charge.status, charge.finished_at = 'refunded', utcnow()
                self.entry(session, account, charge.amount, 'refund', charge.id+':refund', actor, reason)
                detail['charge'] = charge.id
            else:
                raise UserError('Неизвестная операция.')
            session.add(CommerceAudit(key=key, actor=actor, user_id=uid, action=action, detail=detail))

    async def dashboard(self, uid=None):
        async with self.sessions() as session:
            plans = list(await session.scalars(select(Plan).order_by(Plan.id.desc()).limit(100)))
            people = (await session.execute(select(User.telegram_id, func.coalesce(UserProfile.display_name, User.first_name).label("name"),
                func.coalesce(UserProfile.username, User.username).label("username"), Account.balance, Account.plan_id,
                Account.expires_at, Account.role).outerjoin(Account, Account.user_id == User.telegram_id).outerjoin(UserProfile, UserProfile.user_id == User.telegram_id)
                .order_by(User.telegram_id).limit(100))).mappings().all()
            result = dict(plans=plans, people=people, uid=uid, account=None, policy=None, person=None)
            if uid:
                user = await session.get(User, uid)
                profile = await session.get(UserProfile, uid)
                result['person'] = (profile.display_name if profile and profile.display_name else user.first_name or user.username) if user else None
                result['account'] = await session.get(Account, uid)
                result['policy'] = await limits(session, uid, self.settings)
                if result['policy']['plan_id'] is None:
                    for field, override in [('stories', 'stories_override'), ('manual_daily', 'manual_override'), ('llm_daily', 'llm_override')]:
                        if not result['account'] or getattr(result['account'], override) is None:
                            result['policy'][field] = None  # Base values come from bot environment, not admin env.
                if result['account'] and result['account'].plan_id and not any(p.id == result['account'].plan_id for p in plans):
                    assigned = await session.get(Plan, result['account'].plan_id)
                    if assigned:
                        plans.append(assigned)
            for name, model in [('ledger', CreditEntry), ('orders', TopUp), ('charges', Charge), ('audit', CommerceAudit)]:
                query = select(model)
                if uid:
                    query = query.where(model.user_id == uid)
                result[name] = list(await session.scalars(query.order_by(model.created_at.desc()).limit(100)))
            result['groups'] = (await session.execute(select(Charge.plan_id, Charge.operation, Charge.status,
                func.count().label('count'), func.count(func.distinct(Charge.user_id)).label('users'),
                func.sum(Charge.amount).label('credits')).group_by(Charge.plan_id, Charge.operation, Charge.status))).mappings().all()
            result['payments'] = (await session.execute(select(TopUp.plan_id, TopUp.status, TopUp.currency, func.count().label('count'),
                func.sum(TopUp.amount_minor).label('amount')).group_by(TopUp.plan_id, TopUp.status, TopUp.currency))).mappings().all()
            from sqlalchemy import case
            known = (UsageEvent.cost_source == 'provider') & (UsageEvent.currency == 'USD') & UsageEvent.actual_cost.is_not(None)
            result['costs'] = (await session.execute(select(Charge.plan_id,
                func.count().label('calls'), func.sum(case((known, UsageEvent.actual_cost), else_=None)).label('usd'),
                func.sum(case((known, 0), else_=1)).label('unknown'))
                .join(UsageEvent, UsageEvent.request_id == Charge.id).where(UsageEvent.operation == 'llm')
                .group_by(Charge.plan_id))).mappings().all()
            return result


def operation_key():
    return uuid4().hex
