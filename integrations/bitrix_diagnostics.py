"""Read-only Bitrix diagnostics; output contains no task text, names or webhook."""
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

from integrations.sources import Bitrix
from worklog.bitrix_pipeline import normalize


SAFE_REASONS = {
    'Нет события создания: исходное состояние не подтверждено',
    'Неполное изменение истории Bitrix', 'Нет исполнителя задачи',
    'Неизвестный статус задачи', 'Разрыв истории изменений Bitrix',
    'История не совпадает с текущим состоянием задачи',
    'Некорректный timestamp Bitrix', 'Некорректная дата Bitrix',
    'Неизвестный формат даты Bitrix',
}


def sanitized(raw):
    """Keep only structural/date data and replace all account IDs consistently."""
    users = {}
    def account(value):
        if isinstance(value, dict):
            value = value.get('id', value.get('ID'))
        if value in (None, ''):
            return value
        if not isinstance(value, (str, int)) or not str(value).isdigit():
            return {'unsupported_type': type(value).__name__}
        key = str(value)
        if key not in users:
            users[key] = str(900001 + len(users))
        return users[key]

    def date_or_number(value):
        if value in (None, ''):
            return value
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            if re.fullmatch(r'[0-9TZtz+: .\-]{1,40}', str(value)):
                return value
        return {'unsupported_type': type(value).__name__}

    task = raw['task']
    result = {'task': {k: date_or_number(task.get(k)) for k in (
        'id', 'createdDate', 'deadline', 'status', 'realStatus', 'closedDate', 'statusChangedDate')},
        'history': [], 'results': []}
    result['task']['responsibleId'] = account(task.get('responsibleId'))
    for event in raw['history']:
        field = event.get('field')
        if field not in ('NEW', 'RESPONSIBLE_ID', 'STATUS', 'REAL_STATUS', 'DEADLINE'):
            continue
        item = {k: date_or_number(event.get(k)) for k in ('id', 'createdDate')}
        item['field'] = field
        value = event.get('value')
        if isinstance(value, dict):
            convert = account if field == 'RESPONSIBLE_ID' else date_or_number
            item['value'] = {k: convert(value[k]) for k in ('from', 'to') if k in value}
        else:
            item['value'] = {'unsupported_type': type(value).__name__}
        result['history'].append(item)
    return result


def diagnose(raw):
    try:
        normalize(raw, {})
        outcome = {'normalization': 'succeeded'}
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        code = getattr(error, 'code', None)
        reason = str(error) if code or str(error) in SAFE_REASONS else 'Неподдерживаемая структура или дата'
        outcome = {'normalization': 'failed', 'reason': reason, 'reason_code': code}
    return {**outcome, 'history_fields': dict(Counter(e.get('field', 'unknown') for e in raw['history'])),
            'fixture': sanitized(raw)}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args or len(args) > 10 or any(not value.isdigit() or int(value) < 1 for value in args):
        print('Укажите от 1 до 10 номеров задач', file=sys.stderr)
        return 1
    base = os.environ.get('BITRIX_WEBHOOK_URL', '')
    if not base:
        print('В контейнере не задан BITRIX_WEBHOOK_URL', file=sys.stderr)
        return 1
    folder = Path('/data/bitrix-live/diagnostics')
    folder.mkdir(parents=True, exist_ok=True)
    failed = False
    for value in args:
        try:
            result = diagnose(Bitrix(base).task(int(value)))
            path = folder / f'task-{int(value)}.json'
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'task_id': int(value), **{k: v for k, v in result.items() if k != 'fixture'},
                              'saved': f'data/bitrix-live/diagnostics/{path.name}'}, ensure_ascii=False), flush=True)
        except (ValueError, KeyError, TypeError, OSError):
            failed = True
            print(json.dumps({'task_id': int(value), 'error': 'Не удалось получить диагностические данные'}), flush=True)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
