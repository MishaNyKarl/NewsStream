from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re

FIELDS = {
    'vps': 'VPS в месяц, ₽', 'other': 'Прочие расходы в месяц, ₽',
    'llm': 'Бюджет LLM на месяц, $ (сценарий)', 'fx': 'Рублей за 1 $',
    'payers': 'Платящих пользователей', 'margin': 'Целевая маржа, % выручки',
    'fee': 'Комиссия платежей, % выручки', 'tax': 'Налоги, % выручки',
    'price': 'Цена для сценария безубыточности, ₽',
    'input_rate': 'Оценка за 1 млн входных токенов, $',
    'output_rate': 'Оценка за 1 млн выходных токенов, $',
}


def validate(values):
    clean = {}
    for key in FIELDS:
        raw = str(values.get(key, '')).strip().replace(',', '.')
        if not raw:
            clean[key] = ''
            continue
        try:
            if not re.fullmatch(r'\d{1,10}(\.\d{1,9})?', raw):
                raise ValueError
            number = Decimal(raw)
            if not number.is_finite() or number < 0 or number > 1_000_000_000 or len(raw) > 30:
                raise ValueError
            if key in {'margin', 'fee', 'tax'} and number >= 100:
                raise ValueError
            if key in {'fx', 'payers', 'price'} and number == 0:
                raise ValueError
            if key == 'payers' and number != number.to_integral_value():
                raise ValueError
        except (InvalidOperation, ValueError):
            raise ValueError(f'Проверьте поле «{FIELDS[key]}»') from None
        clean[key] = str(number)
    if all(clean[k] for k in ('margin', 'fee', 'tax')):
        if sum(Decimal(clean[k]) for k in ('margin', 'fee', 'tax')) >= 100:
            raise ValueError('Сумма маржи, комиссии и налогов должна быть меньше 100%')
    return clean


def calculate(values):
    clean = validate(values)
    required = ('vps', 'other', 'llm', 'fx', 'payers', 'margin', 'fee', 'tax')
    missing = [FIELDS[k] for k in required if clean[k] == '']
    if missing:
        return {'missing': missing}
    v = {k: Decimal(n) for k, n in clean.items() if n != ''}
    fixed = v['vps'] + v['other']
    llm_rub = v['llm'] * v['fx']
    total = fixed + llm_rub
    net = 1 - (v['fee'] + v['tax']) / 100
    target = net - v['margin'] / 100
    per_user = llm_rub / v['payers']
    result = {'total': total, 'per_user': per_user,
              'breakeven': total / v['payers'] / net,
              'target': total / v['payers'] / target, 'scenarios': []}
    for count in sorted({max(1, int(v['payers']/2)), int(v['payers']), int(v['payers']*2)}):
        scenario_total = fixed + per_user * count
        result['scenarios'].append({'count': count, 'total': scenario_total,
                                    'breakeven': scenario_total / count / net,
                                    'target': scenario_total / count / target})
    if 'price' in v:
        contribution = v['price'] * net - per_user
        result['needed_users'] = (max(1, int((fixed / contribution).to_integral_value(rounding=ROUND_CEILING)))
                                  if contribution > 0 else None)
    return result
