"""Create deterministic, fictional input for offline manual checks."""

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREATED = '2026-09-20T10:00:00+05:00'


def task(number, title, employee='EMP_001', deadline=None, completed=None, extra=()):
    events = [
        {'at': CREATED, 'type': 'assignment', 'employee': employee},
        {'at': CREATED, 'type': 'status', 'value': 'in_progress'},
    ]
    if deadline:
        events.append({'at': CREATED, 'type': 'deadline', 'value': deadline})
    if completed:
        events.append({'at': completed, 'type': 'status', 'value': 'completed'})
    events.extend(extra)
    return {'key': f'demo:offline:{number}', 'title': title, 'created_at': CREATED,
            'history_complete': True, 'events': events}


def main():
    parser = argparse.ArgumentParser(description='Вымышленные данные без внешних подключений')
    parser.add_argument('--output', default='data/offline-demo')
    args = parser.parse_args()
    folder = (ROOT / args.output).resolve()
    try:
        folder.relative_to(ROOT)
    except ValueError:
        parser.error('Папка данных должна находиться внутри проекта')
    if folder == ROOT:
        parser.error('Нельзя использовать корень проекта как папку данных')
    if folder.exists():
        parser.error('Папка уже существует. Выберите новую: перезапись не выполняется.')
    tasks = [
        task(101, 'Закрыта до срока', deadline='2026-10-10T18:00:00+05:00',
             completed='2026-10-09T12:00:00+05:00'),
        task(102, 'Открыта и просрочена в октябре', deadline='2026-10-15T18:00:00+05:00'),
        task(103, 'Просрочена ещё в сентябре', deadline='2026-09-25T18:00:00+05:00'),
        task(104, 'Повторно открыта', completed='2026-10-08T12:00:00+05:00', extra=[
            {'at': '2026-10-09T12:00:00+05:00', 'type': 'status', 'value': 'in_progress'}]),
        task(105, 'Срок перенесли до нарушения', deadline='2026-10-10T18:00:00+05:00', extra=[
            {'at': '2026-10-09T12:00:00+05:00', 'type': 'deadline',
             'value': '2026-11-05T18:00:00+05:00'}]),
        task(106, 'Срок перенесли после нарушения', deadline='2026-10-10T18:00:00+05:00', extra=[
            {'at': '2026-10-12T12:00:00+05:00', 'type': 'deadline',
             'value': '2026-11-05T18:00:00+05:00'}]),
        task(107, 'Закрыта только в ноябре', deadline='2026-10-20T18:00:00+05:00',
             completed='2026-11-02T12:00:00+05:00'),
        task(108, 'Задача другого сотрудника', employee='EMP_002'),
    ]
    incomplete = task(109, 'История недоступна')
    incomplete['history_complete'] = False
    # Expected values are fixed worked examples, never calculated by the application under test.
    expected = {
        'employee': 'EMP_001', 'start': '2026-10-01', 'end': '2026-10-31',
        'counts': {'assigned': 7, 'completed': 1, 'overdue': 3, 'current': 6},
        'completed': ['demo:offline:101'],
        'overdue': ['demo:offline:102', 'demo:offline:106', 'demo:offline:107'],
        'current': ['demo:offline:102', 'demo:offline:103', 'demo:offline:104',
                    'demo:offline:105', 'demo:offline:106', 'demo:offline:107'],
        'partial': False,
    }
    folder.mkdir(parents=True)
    for name, value in (
        ('tasks.json', tasks), ('tasks-incomplete.json', [incomplete]),
        ('expected.json', expected), ('config.json', {'source_file': 'tasks.json', 'demo': True}),
    ):
        with (folder / name).open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    for source in (ROOT / 'examples' / 'manual').iterdir():
        if source.is_file():
            with (folder / source.name).open('xb') as stream:
                stream.write(source.read_bytes())
    print(json.dumps({'folder': str(folder.relative_to(ROOT)), 'tasks': len(tasks),
                      'demo': True, 'external_requests': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
