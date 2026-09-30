"""Notification journal periods and stable, bounded navigation parameters."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain import UserError

MSK = timezone(timedelta(hours=3))
PAGE_SIZE = 8
DATE_PROMPT = '📅 Период журнала'
DATE_HELP = ('Напишите одну дату или две даты через пробел, например: 23.09.2026 30.09.2026. '
             'Обе даты включаются, время — МСК. Для отмены откройте /journal.')


@dataclass(frozen=True)
class JournalWindow:
    start: int
    end: int

    def __post_init__(self):
        if not 0 <= self.start < self.end <= 9_999_999_999:
            raise ValueError('Invalid journal window')

    @property
    def since(self):
        return datetime.fromtimestamp(self.start, timezone.utc)

    @property
    def until(self):
        return datetime.fromtimestamp(self.end, timezone.utc)

    @property
    def label(self):
        end = self.until.astimezone(MSK)
        if not self.start:
            return f'Всё время · по {end:%d.%m.%Y %H:%M} МСК'
        start = self.since.astimezone(MSK)
        if start.time().isoformat() == end.time().isoformat() == '00:00:00':
            last = end - timedelta(seconds=1)
            return (f'{start:%d.%m.%Y}' if start.date() == last.date() else
                    f'{start:%d.%m.%Y} — {last:%d.%m.%Y}') + ' · МСК'
        return f'{start:%d.%m.%Y %H:%M} → {end:%d.%m.%Y %H:%M} МСК'

    def callback(self, action='jn', cursor=0, update_id=None):
        prefix = f'{action}:{update_id}:' if update_id is not None else f'{action}:'
        return f'{prefix}{self.start}:{self.end}:{cursor}'


def preset_window(period='week', now=None):
    now = now or datetime.now(timezone.utc)
    end = int(now.timestamp()) + 1
    if period == 'all':
        start = 0
    elif period == 'today':
        start = int(now.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    elif period in {'day', 'week', 'month'}:
        start = end - {'day': 1, 'week': 7, 'month': 30}[period] * 86400
    else:
        raise ValueError('Unknown journal period')
    return JournalWindow(start, end)


def date_window(value, now=None):
    now = now or datetime.now(timezone.utc)
    parts = re.split(r'\s*[—–]\s*|\s+-\s+|\s+', value.strip())
    try:
        if len(parts) not in {1, 2} or not all(re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', p) for p in parts):
            raise ValueError
        dates = [datetime.strptime(p, '%d.%m.%Y').replace(tzinfo=MSK) for p in parts]
        first, last = dates[0], dates[-1]
        if first.year < 1970 or first > last or last.date() > now.astimezone(MSK).date():
            raise ValueError
        return JournalWindow(int(first.timestamp()), min(int((last + timedelta(days=1)).timestamp()), int(now.timestamp()) + 1))
    except (ValueError, OverflowError):
        raise UserError('Не удалось разобрать период. Укажите существующие даты от 1970 года до сегодня, '
                        'сначала раннюю. ' + DATE_HELP) from None


def parse_journal_callback(value):
    match = re.fullmatch(r'(jn|ju|jo):(?:(\d{1,10}):)?(\d{1,10}):(\d{1,10}):(\d{1,10})', value or '')
    if not match:
        return None
    action, raw_id, start, end, cursor = match.groups()
    if (action == 'jn') != (raw_id is None):
        return None
    update_id, cursor = int(raw_id or 0), int(cursor)
    if max(update_id, cursor) > 2_147_483_647 or (action != 'jn' and update_id == 0):
        return None
    try:
        return action, JournalWindow(int(start), int(end)), cursor, update_id
    except ValueError:
        return None
