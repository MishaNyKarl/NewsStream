"""Shared, durable kill switch, checked before claims and paid requests."""
from sqlalchemy import select, text, or_, and_
from app.models import EconomyState, User, Account, CommerceAudit, utcnow
from app.commerce import Commerce
from app.errors import UserError

NOTICE = '🟠 Включён режим экономии. Бот временно доступен только администраторам. Ваши новости и подписки сохранены.'


def permitted(column, admin_ids=()):
    enabled = select(EconomyState.id).where(EconomyState.enabled.is_(True)).exists()
    admins = select(User.telegram_id).outerjoin(Account, Account.user_id == User.telegram_id).where(
        or_(Account.role == 'admin', and_(or_(Account.user_id.is_(None), Account.role == 'inherit'),
            or_(User.is_admin.is_(True), User.telegram_id.in_(tuple(admin_ids))))))
    return or_(~enabled, column.in_(admins))


class Economy:
    def __init__(self, sessions):
        self.sessions = sessions

    async def snapshot(self):
        async with self.sessions() as session:
            row = await session.get(EconomyState, 1)
            return {'enabled': bool(row and row.enabled), 'version': row.version if row else 0}

    async def allowed(self, uid, admin_ids=()):
        async with self.sessions() as session:
            return bool(await session.scalar(select(User.telegram_id).where(
                User.telegram_id == uid, permitted(User.telegram_id, admin_ids))))

    async def save(self, enabled, version, actor, key):
        async with Commerce(self.sessions, None).transaction() as session:
            if session.bind.dialect.name == 'postgresql':
                await session.execute(text('SELECT pg_advisory_xact_lock(419281706)'))
            if await session.scalar(select(CommerceAudit.id).where(CommerceAudit.key == key)):
                return
            row = await session.get(EconomyState, 1)
            if str(row.version if row else 0) != str(version):
                raise UserError('Режим уже изменился. Обновите страницу.')
            before = bool(row and row.enabled)
            if row is None:
                session.add(EconomyState(id=1, enabled=enabled, version=1, updated_by=actor))
            else:
                row.enabled, row.version, row.updated_by, row.updated_at = enabled, row.version+1, actor, utcnow()
            session.add(CommerceAudit(key=key, actor=actor, action='economy_mode',
                detail={'before': before, 'enabled': enabled}))
