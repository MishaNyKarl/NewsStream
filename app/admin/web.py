import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import hmac
import csv
import io
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from argon2.exceptions import VerificationError
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.responses import Response
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from app.admin.calculator import FIELDS, calculate, validate
from app.admin.config import Config
from app.admin.data import Data, Filters, MSK, identifier
from app.admin.ops import Ops
from app.admin.password import HASHER
from app.admin.state import State
from app.admin.product import report as product_report

COOKIE = '__Host-newswatch-admin'
ROOT = Path(__file__).parent
TITLES = {'/': 'Обзор', '/users': 'Пользователи', '/stories': 'Наблюдения', '/costs': 'Расходы LLM',
          '/analytics': 'Аналитика продукта',
          '/checks': 'Проверки', '/errors': 'Ошибки', '/notifications': 'Уведомления',
          '/calculator': 'Калькулятор', '/project': 'Проект и сервер', '/docs': 'Документация', '/audit': 'Журнал действий'}


def display(value):
    if value is None:
        return '—'
    if isinstance(value, datetime):
        return value.astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S')
    if isinstance(value, bool):
        return 'да' if value else 'нет'
    return redact_doc(str(value))


def redact_doc(value):
    value = re.sub(r'(?i)(postgres(?:ql)?(?:\+asyncpg)?://)[^\s\)"<>]+', r'\1[скрыто]', value)
    value = re.sub(r'\b\d{5,15}:[A-Za-z0-9_-]{25,}\b', '[токен скрыт]', value)
    value = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', '[ключ скрыт]', value)
    return re.sub(r'(?im)^([^\n]*(?:PASSWORD|API_KEY|BOT_TOKEN|CLAIM_TOKEN)\s*=)[^\n]+', r'\1[скрыто]', value)


class Security:
    def __init__(self, app, config, state):
        self.app, self.config, self.state = app, config, state

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request = Request(scope)
        async def secured_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', [])) + [
                    (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff'),
                    # no-referrer makes native form POSTs send Origin: null.
                    # Keep same-site form provenance while hiding referrers from other sites.
                    (b'x-frame-options', b'DENY'), (b'referrer-policy', b'same-origin'),
                    (b'content-security-policy', b"default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
                    (b'strict-transport-security', b'max-age=31536000'),
                    (b'permissions-policy', b'camera=(), microphone=(), geolocation=()'),
                ]
            await send(message)
        expected = urlsplit(self.config.origin).netloc.lower()
        if request.headers.get('host', '').lower() != expected:
            return await PlainTextResponse('Недопустимый адрес', 400)(scope, receive, secured_send)
        if scope.get('scheme') != 'https':
            return await PlainTextResponse('Требуется HTTPS', 403)(scope, receive, secured_send)
        if scope['method'] not in {'GET', 'HEAD', 'POST'}:
            return await PlainTextResponse('Метод запрещён', 405)(scope, receive, secured_send)
        session = self.state.session(request.cookies.get(COOKIE), self.config.session_seconds, self.config.idle_seconds)
        scope.setdefault('state', {})['admin_session'] = session
        public = request.url.path in {'/login', '/healthz'} or request.url.path.startswith('/static/')
        if not public and (not session or not session['authenticated']):
            response = (RedirectResponse('/login', 303) if scope['method'] in {'GET', 'HEAD'}
                        else PlainTextResponse('Требуется вход', 401))
            return await response(scope, receive, secured_send)
        if scope['method'] == 'POST':
            if request.headers.get('origin') != self.config.origin:
                return await PlainTextResponse('Недопустимый источник запроса', 403)(scope, receive, secured_send)
            if request.headers.get('sec-fetch-site', 'same-origin') not in {'same-origin', 'none'}:
                return await PlainTextResponse('Недопустимый источник запроса', 403)(scope, receive, secured_send)
        await self.app(scope, receive, secured_send)


def create_app(config=None, data=None, ops=None):
    config = config or Config.from_env()
    state = State(config.state_path, config.username+config.password_hash)
    data = data or Data(config.database_url)
    ops = ops or Ops(config.ops_socket)
    login_lock = asyncio.Lock()  # At most one 64 MiB Argon2 operation in flight.
    templates = Environment(loader=FileSystemLoader(ROOT/'templates'), autoescape=select_autoescape())
    templates.filters['display'] = display
    templates.filters['money'] = lambda v: '—' if v is None else f'{v:.6f}'
    templates.filters['rub'] = lambda v: '—' if v is None else f'{v:.2f}'
    templates.filters['timestamp'] = lambda v: display(datetime.fromtimestamp(v, MSK))
    templates.filters['duration'] = lambda v: '—' if v is None else (
        f'{v/86400:.1f} дн.' if v >= 86400 else f'{v/3600:.1f} ч' if v >= 3600 else f'{v/60:.1f} мин')
    templates.filters['percent'] = lambda v: '—' if v is None else f'{v:.1f}%'

    def render(request, template, status=200, **context):
        session = request.state.admin_session
        context.update(request=request, title=TITLES.get(request.url.path, 'NewsStream'), nav=TITLES,
                       csrf=session['csrf'] if session else '', authenticated=bool(session and session['authenticated']))
        return HTMLResponse(templates.get_template(template).render(**context), status_code=status)

    def cookie(response, token):
        response.set_cookie(COOKIE, token, max_age=config.session_seconds, secure=True,
                            httponly=True, samesite='strict', path='/')
        return response

    async def form(request):
        if request.headers.get('content-type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            raise HTTPException(415, 'Неверный формат формы')
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 8192:
                raise HTTPException(413, 'Слишком большая форма')
        try:
            parsed = parse_qs(raw.decode('utf-8'), keep_blank_values=True, max_num_fields=30, errors='strict')
            if any(len(v) != 1 for v in parsed.values()):
                raise ValueError
            values = {k: v[0] for k, v in parsed.items()}
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'Неверная форма') from None
        session = request.state.admin_session
        if not session or not hmac.compare_digest(values.get('csrf', '').encode(), session['csrf'].encode()):
            raise HTTPException(403, 'Форма устарела. Обновите страницу.')
        return values

    async def login(request):
        if request.method == 'GET':
            if request.state.admin_session and request.state.admin_session['authenticated']:
                return RedirectResponse('/', 303)
            token, csrf = state.new_session(old_token=request.cookies.get(COOKIE))
            request.state.admin_session = {'csrf': csrf, 'authenticated': False}
            return cookie(render(request, 'login.html'), token)
        values = await form(request)
        ip = request.client.host if request.client else 'unknown'
        if not state.admit_login(ip):
            return render(request, 'login.html', status=429, error='Слишком много попыток. Подождите 15 минут.')
        password = values.get('password', '')
        valid = False
        if len(password) <= 1024:
            async with login_lock:
                try:
                    valid = await run_in_threadpool(HASHER.verify, config.password_hash, password)
                except VerificationError:
                    pass
        valid = valid and hmac.compare_digest(values.get('username', '').encode(), config.username.encode())
        state.audit(config.username, 'login', result='ok' if valid else 'denied')
        if not valid:
            return render(request, 'login.html', status=401, error='Неверный логин или пароль.')
        token, _ = state.new_session(True, request.cookies.get(COOKIE))
        return cookie(RedirectResponse('/', 303), token)

    async def logout(request):
        await form(request)
        state.logout(request.cookies.get(COOKIE, ''))
        state.audit(config.username, 'logout')
        response = RedirectResponse('/login', 303)
        response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
        return response

    async def dashboard(request):
        try:
            filters = Filters.parse(request.query_params)
        except ValueError as exc:
            return render(request, 'message.html', status=400, message=str(exc))
        name = request.url.path.strip('/')
        common = {'filters': filters.form, 'page': filters.page,
                  'previous': str(request.url.include_query_params(page=filters.page-1)),
                  'next': str(request.url.include_query_params(page=filters.page+1))}
        try:
            async with asyncio.timeout(12):
                if not name:
                    health = await ops.status()
                    overview = await data.overview(filters)
                    return render(request, 'overview.html', overview=overview, health=health, **common)
                if name == 'costs':
                    costs = await data.costs(filters, state.calculator())
                    return render(request, 'costs.html', costs=costs, more=costs['more'], **common)
                headers, rows, more = await data.table(name, filters)
                return render(request, 'table.html', headers=headers, rows=rows, more=more, **common)
        except Exception:
            # Never reflect SQL exceptions, connection strings, statement parameters or tracebacks.
            return render(request, 'message.html', status=503,
                          message='База недоступна или схема не соответствует приложению. Данные неизвестны. Проверьте конфигурацию и миграции.')

    async def calculator(request):
        values = state.calculator()
        error = None
        status = 200
        if request.method == 'POST':
            raw = await form(request)
            try:
                values = validate(raw)
                state.save_calculator(values, config.username)
                return RedirectResponse('/calculator', 303)
            except ValueError as exc:
                values, error, status = {k: raw.get(k, '') for k in FIELDS}, str(exc), 400
        return render(request, 'calculator.html', status=status, fields=FIELDS, values=values,
                      error=error, result=calculate(values) if not error else {})

    async def analytics(request):
        try:
            filters = Filters.parse(request.query_params)
            include_admins = request.query_params.get('admins') == '1'
            async with asyncio.timeout(15):
                result = await product_report(data, filters, include_admins)
            if request.url.path.endswith('.csv'):
                if result.get('unavailable'):
                    raise ValueError('Сбор аналитики ещё не включён. Нужна миграция 0007.')
                stream = io.StringIO(newline='')
                writer = csv.writer(stream, delimiter=';')
                writer.writerow(['telegram_id', 'is_admin', 'registered_at_msk', 'actions', 'active_days',
                    'sessions', 'median_observed_span_seconds', 'median_return_gap_seconds', 'last_observed_action_msk',
                    'confirmed_usd', 'estimated_usd', 'unknown_calls', 'period_start', 'period_end_exclusive', 'coverage_start'])
                for row in result['users']:
                    writer.writerow([row['user_id'], row['admin'], display(row['created_at']), row['actions'],
                        row['active_days'], row['sessions'], row['median_span'], row['median_gap'],
                        display(row['last_action']), row['billing']['confirmed'], row['billing']['estimated'],
                        row['billing']['unknown_calls'], filters.start.isoformat(), result['cutoff'].isoformat(),
                        result['coverage'].isoformat()])
                state.audit(config.username, 'analytics_export', result='ok')
                return Response('\ufeff'+stream.getvalue(), media_type='text/csv; charset=utf-8',
                                headers={'Content-Disposition': 'attachment; filename="newsstream-analytics.csv"'})
            start = (filters.page-1)*50
            return render(request, 'analytics.html', result=result, filters=filters.form, include_admins=include_admins,
                          users=result.get('users', [])[start:start+50], sessions=result.get('sessions', [])[:50],
                          requests=result.get('requests', [])[:50], page=filters.page,
                          more=len(result.get('users', [])) > start+50,
                          previous=str(request.url.include_query_params(page=filters.page-1)),
                          next=str(request.url.include_query_params(page=filters.page+1)),
                          export='/analytics.csv?'+str(request.query_params))
        except ValueError as exc:
            return render(request, 'message.html', status=400, message=str(exc))
        except Exception:
            return render(request, 'message.html', status=503,
                          message='Аналитика недоступна. Проверьте миграцию 0007 и SELECT-права на product_events и analytics_state.')

    async def project(request):
        status = await ops.status()
        return render(request, 'project.html', health=status, config=config,
                      provider=identifier(config.provider), model=identifier(config.model),
                      restart_enabled=bool(config.ops_socket and status.get('result') == 'ok'))

    async def restart_prepare(request):
        values = await form(request)
        target = values.get('target')
        if target not in {'bot', 'worker'} or not config.ops_socket:
            raise HTTPException(400, 'Действие недоступно')
        nonce = state.nonce(request.state.admin_session['id'], target)
        return render(request, 'restart.html', target=target, nonce=nonce)

    async def restart(request):
        values = await form(request)
        target = values.get('target', '')
        if (target not in {'bot', 'worker'} or not config.ops_socket
                or values.get('confirm') != f'ПЕРЕЗАПУСТИТЬ {target}'):
            raise HTTPException(400, 'Подтверждение не совпадает')
        if not state.consume(values.get('nonce', ''), request.state.admin_session['id'], target, config.username):
            raise HTTPException(409, 'Подтверждение уже использовано или истекло. Сначала проверьте состояние сервиса.')
        result = await ops.restart(target, values['nonce'])
        code = result.get('result')
        messages = {'completed': 'Команда перезапуска завершена. Проверьте healthcheck через 1–2 минуты.',
                    'cooldown': 'Перезапуск отклонён: действует пауза между операциями.',
                    'failed': 'Перезапуск не выполнен. Проверьте host helper на сервере.',
                    'unavailable': 'Host helper недоступен.',
                    'unknown': 'Результат неизвестен. Не повторяйте операцию до проверки состояния сервиса.',
                    'started': 'Операция уже принята. Проверьте состояние сервиса.'}
        if code not in messages:
            code = 'unknown'
        state.audit(config.username, 'restart_result', target, code)
        return render(request, 'message.html', message=messages[code], request_id=values['nonce'])

    async def docs(request):
        root = config.docs_path.resolve()
        allowed = {p.name: p for p in root.glob('*.md') if p.is_file() and p.resolve().parent == root}
        name = request.query_params.get('name', '')
        content = None
        if name:
            if name not in allowed or allowed[name].stat().st_size > 500000:
                raise HTTPException(404, 'Документ не найден')
            content = redact_doc(allowed[name].read_text(encoding='utf-8'))
        return render(request, 'docs.html', documents=sorted(allowed), document=name, content=content)

    async def audit(request):
        try:
            page = int(request.query_params.get('page', '1'))
            if not 1 <= page <= 10000:
                raise ValueError
        except ValueError:
            raise HTTPException(400, 'Неверная страница') from None
        rows = state.audit_rows(page)
        return render(request, 'audit.html', rows=rows[:50], more=len(rows)>50, page=page,
                      previous=f'/audit?page={page-1}', next=f'/audit?page={page+1}')

    async def error(request, exc):
        return render(request, 'message.html', status=exc.status_code, message=exc.detail)

    async def health(request):
        return PlainTextResponse('ok')  # Process liveness only, no public operational data.

    @asynccontextmanager
    async def lifespan(app):
        yield
        await data.close()

    routes = [Route('/login', login, methods=['GET', 'POST']), Route('/logout', logout, methods=['POST']),
              Route('/healthz', health), Route('/calculator', calculator, methods=['GET', 'POST']),
              Route('/project', project), Route('/restart/prepare', restart_prepare, methods=['POST']),
              Route('/analytics', analytics), Route('/analytics.csv', analytics),
              Route('/restart', restart, methods=['POST']), Route('/docs', docs), Route('/audit', audit),
              Mount('/static', StaticFiles(directory=ROOT/'static'), name='static')]
    routes += [Route(path, dashboard) for path in ('/', '/users', '/stories', '/costs', '/checks', '/errors', '/notifications')]
    app = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: error})
    app.state.admin = state
    return Security(app, config, state)
