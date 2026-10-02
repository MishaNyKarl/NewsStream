import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
import sqlite3
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.admin.calculator import calculate, validate
from app.admin.config import Config
from app.admin.data import Data, Filters, safe_detail
from app.admin.password import HASHER
from app.admin.state import State
from app.admin.web import COOKIE, create_app
from app.models import Base, Story, UsageEvent, User, utcnow

PASSWORD = 'correct horse battery staple!'


@pytest.fixture(scope='session')
def password_hash():
    return HASHER.hash(PASSWORD)


class FakeOps:
    def __init__(self):
        self.calls = []

    async def status(self):
        return {'result': 'unavailable'}

    async def restart(self, target, request_id):
        self.calls.append((target, request_id))
        return {'result': 'completed'}


@pytest.fixture
async def admin(tmp_path, password_hash):
    config = Config(database_url=f'sqlite+aiosqlite:///{tmp_path / "bot.db"}',
                    password_hash=password_hash, origin='https://admin.test',
                    state_path=tmp_path/'admin.db', docs_path=tmp_path/'docs', ops_socket='/test.sock')
    config.docs_path.mkdir()
    (config.docs_path/'SAFE.md').write_text('# Hello <script>alert(1)</script>', encoding='utf-8')
    (tmp_path/'SECRET.md').write_text('hidden', encoding='utf-8')
    data = Data(config.database_url)
    async with data.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ops = FakeOps()
    app = create_app(config, data, ops)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=config.origin,
                               headers={'Origin': config.origin}) as client:
        yield client, app, data, ops, config
    await data.close()


def csrf(response):
    return re.search(r'name="csrf" value="([^"]+)"', response.text)[1]


async def login(client):
    page = await client.get('/login')
    return await client.post('/login', data={'csrf': csrf(page), 'username': 'admin', 'password': PASSWORD})


@pytest.mark.parametrize('path', ['/', '/users', '/stories', '/costs', '/checks', '/errors',
                                   '/notifications', '/project', '/calculator', '/docs', '/audit',
                                   '/analytics', '/analytics.csv'])
async def test_every_private_page_requires_session(admin, path):
    client, *_ = admin
    response = await client.get(path)
    assert response.status_code == 303 and response.headers['location'] == '/login'


async def test_secure_login_rotation_logout_and_headers(admin):
    client, app, *_ = admin
    page = await client.get('/login')
    old = client.cookies.get(COOKIE)
    assert all(flag in page.headers['set-cookie'] for flag in ('Secure', 'HttpOnly', 'SameSite=strict', 'Path=/'))
    response = await client.post('/login', data={'csrf': csrf(page), 'username': 'admin', 'password': PASSWORD})
    assert response.status_code == 303
    assert old != client.cookies.get(COOKIE)
    assert app.state.session(old, 28800, 1800) is None
    page = await client.get('/')
    assert page.status_code == 200
    assert page.headers['cache-control'] == 'no-store'
    assert page.headers['referrer-policy'] == 'same-origin'
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']
    assert 'max-age=' in page.headers['strict-transport-security']
    assert (await client.post('/logout', data={'csrf': csrf(page)})).status_code == 303
    assert (await client.get('/')).status_code == 303
    assert [r['action'] for r in app.state.audit_rows(1)] == ['logout', 'login']


async def test_http_spoofed_proxy_host_and_csrf_are_rejected(admin):
    client, *_ = admin
    assert (await client.get('http://admin.test/login', headers={'X-Forwarded-Proto': 'https'})).status_code == 403
    assert (await client.get('/login', headers={'Host': 'evil.test'})).status_code == 400
    page = await client.get('/login')
    payload = {'csrf': csrf(page), 'username': 'admin', 'password': PASSWORD}
    assert (await client.post('/login', data=payload, headers={'Origin': 'https://evil.test'})).status_code == 403
    assert (await client.post('/login', data=payload, headers={'Origin': 'null'})).status_code == 403
    assert (await client.post('/login', data=payload | {'csrf': 'юникод'})).status_code == 403
    assert (await client.post('/login', data=payload | {'csrf': ''})).status_code == 403
    assert (await client.post('/restart', data=payload)).status_code == 401


async def test_login_throttling_is_persistent_and_not_forwarded_ip(admin):
    client, app, _, _, config = admin
    page = await client.get('/login')
    for i in range(5):
        response = await client.post('/login', data={'csrf': csrf(page), 'username': 'admin', 'password': 'bad'},
                                     headers={'X-Forwarded-For': f'1.2.3.{i}'})
        assert response.status_code == 401
    response = await client.post('/login', data={'csrf': csrf(page), 'username': 'admin', 'password': PASSWORD})
    assert response.status_code == 429
    reopened = State(config.state_path, config.username+config.password_hash)
    assert not reopened.admit_login('127.0.0.1')
    assert len(app.state.audit_rows(1)) == 5


async def test_forms_are_bounded_duplicate_fields_rejected(admin):
    client, *_ = admin
    await client.get('/login')
    for body, expected in [('a='+'a'*8192, 413), ('csrf=a&csrf=b', 400)]:
        response = await client.post('/login', content=body,
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
        assert response.status_code == expected


async def test_restart_nonce_target_binding_csrf_and_replay(admin):
    client, app, _, ops, _ = admin
    await login(client)
    token = csrf(await client.get('/project'))
    response = await client.post('/restart/prepare', data={'csrf': token, 'target': 'bot'})
    nonce = re.search(r'name="nonce" value="([^"]+)"', response.text)[1]
    body = {'csrf': token, 'target': 'bot', 'nonce': nonce, 'confirm': 'ПЕРЕЗАПУСТИТЬ bot'}
    assert (await client.post('/restart', data=body | {'target': 'worker', 'confirm': 'ПЕРЕЗАПУСТИТЬ worker'})).status_code == 409
    assert (await client.post('/restart', data=body | {'csrf': 'bad'})).status_code == 403
    assert (await client.post('/restart', data=body | {'confirm': 'yes'})).status_code == 400
    assert (await client.post('/restart', data=body)).status_code == 200
    assert (await client.post('/restart', data=body)).status_code == 409
    assert ops.calls == [('bot', nonce)]
    assert (await client.post('/restart/prepare', data={'csrf': token, 'target': 'db'})).status_code == 400
    assert {r['action'] for r in app.state.audit_rows(1)} >= {'restart_requested', 'restart_result'}


async def test_expired_and_other_session_nonce_rejected(admin):
    _, app, *_ = admin
    nonce = app.state.nonce('session-a', 'worker')
    assert not app.state.consume(nonce, 'session-b', 'worker', 'admin')
    with app.state.db() as db:
        db.execute('UPDATE nonces SET expires=0')
    assert not app.state.consume(nonce, 'session-a', 'worker', 'admin')


async def test_no_secrets_in_errors_or_docs_and_docs_traversal(admin):
    client, _, data, _, config = admin
    await login(client)
    response = await client.get('/docs?name=SAFE.md')
    assert '&lt;script&gt;' in response.text and '<script>' not in response.text
    for path in ('../SECRET.md', '/etc/passwd', '.env', 'SAFE.md/../SECRET.md'):
        assert (await client.get('/docs', params={'name': path})).status_code == 404
    (config.docs_path/'SECRETS.md').write_text('postgresql+asyncpg://a:secret@db/b\nLLM_API_KEY=sk-test'+'a'*40, encoding='utf-8')
    assert 'secret@db' not in (await client.get('/docs?name=SECRETS.md')).text
    async with data.engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    response = await client.get('/')
    assert response.status_code == 503 and config.database_url not in response.text
    assert 'Traceback' not in response.text


async def test_data_filters_costs_and_empty_unknown(admin):
    client, _, data, _, _ = admin
    now = utcnow()
    async with data.sessions.begin() as s:
        s.add_all([User(telegram_id=1, username='<script>bad</script>'), User(telegram_id=2)])
        await s.flush()
        s.add_all([UsageEvent(user_id=1, operation='llm', input_tokens=1000, output_tokens=500, estimated_cost=0.02),
                   UsageEvent(user_id=2, operation='llm', input_tokens=500, output_tokens=500, estimated_cost=1),
                   UsageEvent(user_id=1, operation='llm_reserved', estimated_cost=7),
                   UsageEvent(user_id=1, operation='llm', estimated_cost=8, created_at=now-timedelta(days=90)),
                   UsageEvent(user_id=1, operation='error', detail='postgresql://secret:pass@host/db')])
    filters = Filters.parse({'user': '1'})
    costs = await data.costs(filters, {'input_rate': '1', 'output_rate': '2'})
    assert costs['total']['calls'] == 1
    assert costs['total']['recorded'] == 0.02
    assert costs['total']['estimate'] == Decimal('0.002')
    assert len(costs['requests']) == 1
    empty = await data.costs(Filters.parse({'user': '999'}), {})
    assert empty['total']['recorded'] is None and empty['total']['estimate'] is None
    await login(client)
    assert '&lt;script&gt;' in (await client.get('/users?user=1')).text
    response = await client.get('/errors?user=1')
    assert 'secret:pass' not in response.text and 'Подробности скрыты' in response.text
    async with data.sessions() as s:
        assert len(list(await s.scalars(select(UsageEvent)))) == 5  # Analytics never writes bot DB.


async def test_pagination_and_live_queue(admin):
    _, _, data, _, _ = admin
    async with data.sessions.begin() as s:
        s.add(User(telegram_id=1))
        await s.flush()
        s.add_all([UsageEvent(operation='error', user_id=1, detail='check: ValueError') for _ in range(53)])
        s.add(Story(user_id=1, title='Topic', original_input='', summary='', current_state='',
                    status='active', next_check_at=utcnow()-timedelta(minutes=1)))
    _, rows, more = await data.table('errors', Filters.parse({}))
    assert len(rows) == 50 and more
    _, rows2, more = await data.table('errors', Filters.parse({'page': '2'}))
    assert len(rows2) == 3 and not more and set(r[0] for r in rows).isdisjoint(r[0] for r in rows2)
    overview = await data.overview(Filters.parse({}))
    assert overview['Проверок ждут сейчас'] == 1 and overview['Проверок в работе'] == 0


@pytest.mark.parametrize('values', [{'user': '1 OR 1=1'}, {'page': '0'}, {'page': '10001'},
                                    {'start': '2026-02-30'}, {'start': '2025-01-01', 'end': '2026-01-02'},
                                    {'start': '2026-01-02', 'end': '2026-01-01'}, {'user': str(2**63)}])
def test_filter_validation(values):
    with pytest.raises(ValueError):
        Filters.parse(values)


def test_moscow_date_boundaries():
    f = Filters.parse({'start': '2026-10-01', 'end': '2026-10-01'})
    assert f.start == datetime(2026, 9, 30, 21, tzinfo=timezone.utc)
    assert f.end == datetime(2026, 10, 1, 21, tzinfo=timezone.utc)


def test_calculator_formula_and_scenarios():
    result = calculate({'vps': '1000', 'other': '200', 'llm': '10', 'fx': '90', 'payers': '30',
                        'fee': '5', 'tax': '5', 'margin': '20', 'price': '100'})
    assert result['total'] == 2100 and result['per_user'] == 30
    assert result['breakeven'] == Decimal(2100)/30/Decimal('.90')
    assert result['target'] == 100
    assert result['needed_users'] == 20
    assert result['scenarios'][0]['total'] == 1650


@pytest.mark.parametrize('values', [{'vps': '-1'}, {'fx': 'NaN'}, {'fx': 'Infinity'}, {'payers': '0'},
                                    {'payers': '1.5'}, {'tax': '100'}, {'margin': '90','fee': '5','tax': '5'},
                                    {'fx': '1e-999999999'}, {'llm': '0.00000000001'}])
def test_calculator_rejects_invalid_numbers(values):
    with pytest.raises(ValueError):
        validate(values)


def test_calculator_missing_does_not_mean_zero():
    assert calculate({})['missing']
    assert calculate({'vps': '0'})['missing']
    assert safe_detail('check: RuntimeError') == 'check: RuntimeError'
    assert safe_detail('Authorization: Bearer sk-secret') == 'Подробности скрыты'


def test_session_expiry_credential_rotation_and_no_plaintext(tmp_path):
    state = State(tmp_path/'state.db', 'credential')
    token, _ = state.new_session(True)
    assert state.session(token, 100, 50)
    assert token.encode() not in (tmp_path/'state.db').read_bytes()
    with state.db() as db:
        db.execute('UPDATE sessions SET touched=0')
    assert state.session(token, 100, 50) is None
    token, _ = state.new_session(True)
    assert State(tmp_path/'state.db', 'rotated').session(token, 100, 50) is None


def test_https_and_fingerprint_configuration(password_hash, tmp_path):
    conf = Config('sqlite+aiosqlite:///test.db', password_hash, 'https://admin.test', tmp_path/'state.db')
    for changes in ({'origin': 'http://admin.test'}, {'origin': 'https://admin.test/'},
                    {'key_fingerprint': 'sk-real-secret'}, {'password_hash': HASHER.hash('a').replace('m=65536', 'm=8192')}):
        with pytest.raises(ValueError):
            replace(conf, **changes)


async def test_calculator_save_requires_csrf_and_is_audited(admin):
    client, app, *_ = admin
    await login(client)
    page = await client.get('/calculator')
    assert (await client.post('/calculator', data={'vps': '100'})).status_code == 403
    assert (await client.post('/calculator', data={'csrf': csrf(page), 'vps': '100'})).status_code == 303
    assert app.state.calculator()['vps'] == '100'
    assert app.state.audit_rows(1)[0]['action'] == 'calculator_saved'


def test_global_login_limit(tmp_path):
    state = State(tmp_path/'state.db', 'credential')
    assert all(state.admit_login(str(i)) for i in range(25))
    assert not state.admit_login('new')


async def test_nonce_atomic_under_parallel_consumers(tmp_path):
    state = State(tmp_path/'state.db', 'credential')
    nonce = state.nonce('s', 'bot')
    results = await asyncio.gather(*(asyncio.to_thread(state.consume, nonce, 's', 'bot', 'admin') for _ in range(8)))
    assert sum(results) == 1


def test_helper_rejects_arbitrary_actions_and_idempotency(tmp_path):
    from admin_ops.helper import Operations, validate as protocol
    for payload in ({'action': 'exec', 'command': 'id'}, {'action': 'restart', 'target': 'db', 'id': str(uuid.uuid4())},
                    {'action': 'restart', 'target': 'bot;id', 'id': str(uuid.uuid4())},
                    {'action': 'status', 'path': '/etc/shadow'}, {'action': 'restart', 'target': 'bot', 'id': '../bad'}):
        with pytest.raises(ValueError):
            protocol(payload)
    calls = []
    def runner(argv, timeout=5):
        calls.append(argv)
        if 'inspect' in argv:
            return '{"id":"'+'a'*64+'","project":"newswatch","service":"bot"}'
        return ''
    ops = Operations(tmp_path/'ops.db', runner)
    payload = {'action': 'restart', 'target': 'bot', 'id': str(uuid.uuid4())}
    assert ops.restart(payload)['result'] == 'completed'
    assert ops.restart(payload)['result'] == 'completed'
    assert len(calls) == 2 and calls[-1] == ['/usr/bin/docker', 'restart', '--time', '20', 'a'*64]
    assert ops.restart(payload | {'id': str(uuid.uuid4())})['result'] == 'cooldown'
    reopened = Operations(tmp_path/'ops.db', runner)
    assert reopened.restart(payload)['result'] == 'completed' and len(calls) == 2


def test_helper_unknown_blocks_retries_even_after_cooldown(tmp_path):
    from admin_ops.helper import Operations
    def failing(argv, timeout=5):
        raise TimeoutError
    ops = Operations(tmp_path/'ops.db', failing)
    payload = {'action': 'restart', 'target': 'worker', 'id': str(uuid.uuid4())}
    assert ops.restart(payload)['result'] == 'unknown'
    with sqlite3.connect(ops.path) as db:
        db.execute('UPDATE operations SET created=0')
    assert ops.restart(payload | {'id': str(uuid.uuid4())})['result'] == 'cooldown'


def test_helper_checks_project_identity_before_restart(tmp_path):
    from admin_ops.helper import Operations
    calls = []
    def runner(argv, timeout=5):
        calls.append(argv)
        return '{"id":"'+'a'*64+'","project":"other-app","service":"bot"}'
    ops = Operations(tmp_path/'ops.db', runner)
    assert ops.restart({'action': 'restart', 'target': 'bot', 'id': str(uuid.uuid4())})['result'] == 'unknown'
    assert len(calls) == 1


async def test_authenticated_pages_render_and_missing_costs_are_not_zero(admin):
    client, *_ = admin
    await login(client)
    for path in ('/', '/users', '/stories', '/costs', '/checks', '/errors', '/notifications',
                 '/project', '/calculator', '/docs', '/audit', '/static/admin.css'):
        response = await client.get(path)
        assert response.status_code == 200, (path, response.text)
    response = await client.get('/costs')
    assert 'Нет данных' in response.text and '0.000000' not in response.text


async def test_helper_concurrency_one_restart(tmp_path):
    from admin_ops.helper import Operations
    calls = []
    def runner(argv, timeout=5):
        calls.append(argv)
        if 'inspect' in argv:
            return '{"id":"'+'b'*64+'","project":"newswatch","service":"bot"}'
        return ''
    ops = Operations(tmp_path/'ops.db', runner)
    responses = await asyncio.gather(*(asyncio.to_thread(ops.restart, {
        'action': 'restart', 'target': 'bot', 'id': str(uuid.uuid4())}) for _ in range(8)))
    assert sum(r['result'] == 'completed' for r in responses) == 1
    assert len(calls) == 2


async def test_healthy_helper_and_restart_confirmation_page(admin):
    client, _, _, ops, _ = admin
    async def healthy():
        return {'result': 'ok', 'services': {'bot': {'status': 'running', 'health': 'healthy', 'restarts': 0}},
                'host': {'memory_available_mb': 800, 'memory_total_mb': 2000, 'disk_free_gb': 10,
                         'uptime_hours': 1, 'load': '0.10'}, 'operations': []}
    ops.status = healthy
    await login(client)
    for path in ('/', '/project'):
        page = await client.get(path)
        assert page.status_code == 200 and 'healthy' in page.text
    assert 'Перезапустить bot' in page.text
    page = await client.post('/restart/prepare', data={'csrf': csrf(page), 'target': 'worker'})
    assert page.status_code == 200 and 'ПЕРЕЗАПУСТИТЬ worker' in page.text


def test_helper_status_never_returns_container_environment(tmp_path, monkeypatch):
    import json
    from admin_ops import helper
    def runner(argv, timeout=5):
        target = next(k for k, name in helper.CONTAINERS.items() if argv[-1] == name)
        return json.dumps({'id': 'a'*64, 'project': 'newswatch', 'service': target,
                           'status': 'running', 'health': 'healthy', 'restarts': 0,
                           'Env': ['LLM_API_KEY=secret-should-not-leak']})
    original = helper.Path.read_text
    def proc(path, *args, **kwargs):
        if str(path).replace('\\', '/') == '/proc/meminfo':
            return 'MemTotal: 2048000 kB\nMemAvailable: 512000 kB\n'
        if str(path).replace('\\', '/') == '/proc/uptime':
            return '7200 1000'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(helper.Path, 'read_text', proc)
    monkeypatch.setattr(helper.os, 'getloadavg', lambda: (0, 0, 0), raising=False)
    result = helper.Operations(tmp_path/'ops.db', runner).status()
    assert result['host']['memory_available_mb'] == 500 and result['host']['uptime_hours'] == 2
    assert 'secret-should-not-leak' not in json.dumps(result)

async def test_product_analytics_dashboard_and_csv(admin):
    from app.models import AnalyticsState, ProductEvent
    client, app, data, *_ = admin
    now = utcnow()
    async with data.sessions() as session:
        session.add(User(telegram_id=999, created_at=now-timedelta(days=2), is_admin=False))
        await session.flush()
        session.add(AnalyticsState(id=1, started_at=now-timedelta(days=3)))
        session.add(ProductEvent(user_id=999, event='interaction_start', created_at=now-timedelta(hours=1)))
        await session.commit()
    await login(client)
    response = await client.get('/analytics')
    assert response.status_code == 200
    assert '999' in response.text and 'D7' in response.text and 'Скачать CSV' in response.text
    export = await client.get('/analytics.csv')
    assert export.status_code == 200 and export.content.startswith(b'\xef\xbb\xbf')
    assert '999;' in export.text
    assert app.state.audit_rows(1)[0]['action'] == 'analytics_export'
    assert (await client.get('/analytics?start=invalid')).status_code == 400


async def test_product_analytics_requires_coverage_marker(admin):
    client, *_ = admin
    await login(client)
    assert 'ещё не собирается' in (await client.get('/analytics')).text
    assert (await client.get('/analytics.csv')).status_code == 400
