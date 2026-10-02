from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.account_ui import account_screen
from app.models import utcnow
from test_bot import Harness
from test_commerce import configured
from test_repository import store as store


async def test_account_navigation_uses_callback_sender():
    h = Harness()
    h.service.account_text = AsyncMock(return_value='Мой тариф')
    await h.message('/account')
    h.service.account_text.assert_awaited_with(100)
    assert 'account:history' in str(h.session.calls)
    for view in ('home', 'prices', 'history', 'manage'):
        h.reset_throttle()
        await h.callback(f'account:{view}')
        h.service.account_text.assert_awaited_with(100, view)


async def test_account_history_is_private_and_hides_admin_notes(store):
    repo, commerce, _ = await configured(store)
    await repo.admit_user(2)
    assert not await commerce.credit_history(2)
    entries = await commerce.credit_history(1)
    assert len(entries) == 1 and entries[0].delta == 10
    snap = await commerce.snapshot(1)
    snap['limits']['name'] = '<Premium & test>'
    assert '&lt;Premium &amp; test&gt;' in account_screen(snap)
    assert 'Test credits' not in account_screen(snap, 'history', entries)
    assert '+10' in account_screen(snap, 'history', entries)
    assert '3 кр.' in account_screen(snap, 'prices')
    snap['limits']['discussion'] = False
    assert 'Не входит в тариф' in account_screen(snap, 'prices')
    snap['limits']['plan_id'] = None
    assert 'подписка закончилась' in account_screen(snap)
    assert 'Онлайн-оплата' in account_screen(snap, 'manage')


def test_history_does_not_expose_arbitrary_kind_or_reason():
    entry = SimpleNamespace(kind='<secret>', reason='private', delta=-2, balance_after=8, created_at=utcnow())
    text = account_screen({'account': None, 'limits': {}}, 'history', [entry])
    assert '<secret>' not in text and 'private' not in text
    assert '-2' in text
