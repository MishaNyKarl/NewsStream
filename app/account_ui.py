"""Private account screens for the Telegram bot."""
from html import escape
from datetime import timedelta, timezone

from aiogram.types import InlineKeyboardButton
from app.navigation import with_home


def account_keyboard(view='home'):
    buttons = [('💎 Мой тариф', 'home'), ('🪙 Стоимость действий', 'prices'),
               ('🧾 История кредитов', 'history'), ('🛍 Тарифы и предложения', 'manage')]
    rows = [[InlineKeyboardButton(text=label, callback_data=f'account:{key}')]
            for label, key in buttons if key != view]
    rows.append([InlineKeyboardButton(text='📋 Мои наблюдения', callback_data='list:0')])
    rows.append([InlineKeyboardButton(text='🛍 Открыть каталог тарифов', callback_data='shop:home')])
    rows.append([InlineKeyboardButton(text='⚙️ Настройки аккаунта', callback_data='settings:home')])
    return with_home(rows)


def account_screen(value, view='home', entries=()):
    account, p = value['account'], value['limits']
    balance = account.balance if account else 0
    heading = f'💎 <b>Подписка и кредиты</b>\n\n🪙 Баланс: <b>{balance} кредитов</b>'
    if view == 'history':
        labels = {'reserve': 'Оплата действия', 'refund': 'Возврат', 'grant': 'Начисление',
                  'set_balance': 'Корректировка', 'topup': 'Пополнение'}
        lines = []
        for e in entries:
            date = e.created_at.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m %H:%M')
            lines.append(f'{date} · {escape(labels.get(e.kind, "Изменение баланса"))}\n'
                         f'<b>{e.delta:+d}</b> кр. → {e.balance_after} кр.')
        return heading + '\n\n<b>Последние 10 изменений · МСК</b>\n\n' + (
            '\n\n'.join(lines) or 'Пока нет изменений баланса.')
    if view == 'manage':
        return heading + ('\n\n<b>Тарифы и предложения</b>\n'
            'Откройте каталог тарифов по кнопке ниже. Онлайн-оплата пока в разработке; '
            'выбор тарифа не списывает деньги и не подключает подписку.')
    if view == 'prices':
        def price(key):
            return f"{p[key]} кр." if p[key] else 'Бесплатно'
        discussion = price('discussion_credits') if p['discussion'] else 'Не входит в тариф'
        return heading + (f'\n\n<b>Стоимость по вашему тарифу</b>\n'
            f"📰 Разбор новости — <b>{price('news_credits')}</b>\n"
            f"🔎 Проверка обновлений — <b>{price('check_credits')}</b>\n"
            f'💬 Ответ в обсуждении — <b>{discussion}</b>\n\n'
            'Проверки по расписанию тоже расходуют кредиты. Завершённая проверка '
            'оплачивается, даже если новых фактов нет. При ошибке кредиты возвращаются.\n\n'
            'Чтобы приостановить проверки, поставьте наблюдение на паузу.')
    expiry = 'Базовый доступ'
    if p['plan_id']:
        expiry = ('До ' + account.expires_at.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')
                  if account.expires_at else 'Без указанного срока окончания')
    elif account and account.plan_id:
        expiry = 'Предыдущая подписка закончилась · действует базовый доступ'
    return heading + (f"\n\n<b>{escape(p['name'])}</b>\n{expiry}\n\n"
        '<b>Ваши возможности</b>\n'
        f"📋 Наблюдения — до <b>{p['stories']}</b>\n"
        f"⚡ Срочные наблюдения — до <b>{p['intensive_slots']}</b> одновременно\n"
        f"🔎 Ручные проверки — <b>{p['manual_daily']}</b> в сутки\n"
        f"💬 Обсуждение новостей — <b>{'доступно' if p['discussion'] else 'не входит'}</b>\n"
        f"📖 Полный отчёт («Читать дальше») — <b>{'доступен' if p.get('full_reports') else 'не входит'}</b>\n"
        f"🤖 Обращения к ИИ — до <b>{p['llm_daily']}</b> в сутки\n\n"
        'Суточные лимиты обновляются в 03:00 МСК. Одно действие может требовать '
        'несколько обращений к ИИ.\n\nВыберите раздел ниже 👇')
