"""Validated runtime knobs. No credentials, network calls or process restarts."""
import re
from sqlalchemy import select, text
from app.commerce import Commerce
from app.errors import UserError
from app.models import CommerceAudit, ReportControl, utcnow

# label, default, lower bound, upper bound
FIELDS = {
    'preview_words': ('Слов в кратком содержании уведомления', 120, 20, 300),
    'full_words': ('Слов в подробном отчёте; 0 — весь сохранённый текст', 0, 0, 3000),
    'digest_words': ('Слов в одной строке обновления ежедневного отчёта', 40, 10, 80),
    'search_queries': ('Поисковых запросов на одну проверку', 6, 1, 6),
    'source_reads': ('Попыток чтения страниц на одну проверку', 10, 1, 12),
    'analysis_sources': ('Источников для анализа моделью', 6, 1, 6),
    'source_words': ('Слов из каждой статьи для модели; 0 — стандартный лимит символов', 0, 0, 1500),
}
DEFAULTS = {key: field[1] for key, field in FIELDS.items()}


def clip_words(value, limit):
    text_value = str(value or '')
    words = list(re.finditer(r'\S+', text_value))
    if not limit or len(words) <= limit:
        return text_value
    return text_value[:words[limit - 1].end()] + '…'


class ReportControls:
    def __init__(self, sessions):
        self.sessions = sessions

    async def snapshot(self, settings=None):
        values = dict(DEFAULTS)
        if settings:
            for key, attr in [('search_queries', 'max_search_queries_per_story'),
                              ('source_reads', 'max_source_reads_per_check'),
                              ('analysis_sources', 'max_sources_per_check')]:
                values[key] = max(FIELDS[key][2], min(FIELDS[key][3], getattr(settings, attr)))
        async with self.sessions() as session:
            row = await session.get(ReportControl, 1)
            if row:
                values.update(row.values)
            return {'values': values, 'version': row.version if row else 0,
                    'updated_by': row.updated_by if row else '', 'updated_at': row.updated_at if row else None}

    @staticmethod
    def validate(payload):
        values = {}
        for name, (label, _, low, high) in FIELDS.items():
            try:
                value = int(payload.get(name, ''))
            except (TypeError, ValueError):
                raise UserError(f'{label}: требуется целое число.') from None
            if not low <= value <= high:
                raise UserError(f'{label}: допустимо от {low} до {high}.')
            values[name] = value
        if values['analysis_sources'] > values['source_reads']:
            raise UserError('Источников для анализа не может быть больше попыток чтения страниц.')
        return values

    async def save(self, payload, actor, key):
        values = self.validate(payload)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}', key or ''):
            raise UserError('Форма устарела. Откройте настройки заново.')
        reason = payload.get('reason', '').strip()
        if not 3 <= len(reason) <= 240:
            raise UserError('Укажите причину изменения (3–240 символов).')
        async with Commerce(self.sessions, None).transaction() as session:
            if session.bind.dialect.name == 'postgresql':
                await session.execute(text('SELECT pg_advisory_xact_lock(419281705)'))
            if await session.scalar(select(CommerceAudit.id).where(CommerceAudit.key == key)):
                return
            row = await session.get(ReportControl, 1)
            if str(row.version if row else 0) != str(payload.get('version', '')):
                raise UserError('Настройки уже изменились. Обновите страницу и повторите.')
            before = row.values if row else dict(DEFAULTS)
            if row is None:
                row = ReportControl(id=1, values=values, version=1, updated_by=actor)
                session.add(row)
            else:
                row.values = values
                row.version += 1
                row.updated_by = actor
                row.updated_at = utcnow()
            session.add(CommerceAudit(key=key, actor=actor, action='report_controls',
                detail={'before': before, 'values': values, 'reason': reason}))
