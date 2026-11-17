"""Private local material and explicit export operations."""

import hashlib
import json
from pathlib import Path

from employees.registry import Registry
from .text import prepare_text, restore_text


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def prepare(source, db, run_dir):
    text = Path(source).read_text(encoding='utf-8-sig')
    registry = Registry(db, read_only=True)
    try:
        records = registry.list()
    finally:
        registry.close()
    candidate, names, findings = prepare_text(text, records)
    folder = Path(run_dir)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'candidate.txt').write_text(candidate, encoding='utf-8')
    state = {'status': ('awaiting_privacy_decision' if findings else 'policy_checked'),
             'version': digest(candidate),
             'names': names, 'findings': findings}
    (folder / 'state.json').write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    return public_state(state)


def load(run_dir):
    folder = Path(run_dir)
    state = json.loads((folder / 'state.json').read_text(encoding='utf-8'))
    if (not isinstance(state, dict)
            or not {'status', 'version', 'names', 'findings'} <= state.keys()
            or state['status'] not in ('policy_checked', 'awaiting_privacy_decision',
                                        'user_approved_exception', 'skipped')
            or not isinstance(state['version'], str)
            or not isinstance(state['names'], dict)
            or not all(isinstance(key, str) and isinstance(value, str)
                       for key, value in state['names'].items())
            or not isinstance(state['findings'], list)):
        raise ValueError('Повреждён файл состояния локального запуска')
    candidate = (folder / 'candidate.txt').read_text(encoding='utf-8')
    if digest(candidate) != state['version']:
        raise ValueError('Материал изменён после проверки. Подготовьте новый запуск.')
    return state, candidate


def public_state(state):
    return {key: state[key] for key in ('status', 'version', 'findings')}


def decide(run_dir, action, version):
    state, _ = load(run_dir)
    if version != state['version']:
        raise ValueError('Версия материала не совпадает с показанной пользователю')
    if state['status'] != 'awaiting_privacy_decision':
        raise ValueError('Эта задача не ожидает решения пользователя')
    state['status'] = 'user_approved_exception' if action == 'continue' else 'skipped'
    state['decision'] = {'action': action, 'version': version}
    folder = Path(run_dir)
    temporary = folder / 'state.tmp'
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(folder / 'state.json')
    return public_state(state)


def export(run_dir, output, bundle=False):
    state, candidate = load(run_dir)
    if state['status'] not in ('policy_checked', 'user_approved_exception'):
        raise ValueError('Отправка не разрешена: требуется решение пользователя')
    with Path(output).open('x', encoding='utf-8', newline='') as stream:
        if bundle:
            json.dump({'protocol': 1, 'status': state['status'], 'version': state['version'],
                       'text': candidate, 'employee_labels': sorted(state['names']),
                       'source_ids': ['SOURCE_001']}, stream, ensure_ascii=False, indent=2)
        else:
            stream.write(candidate)
    return {'status': 'exported'}


def restore(run_dir, answer, output, json_output=False):
    state, _ = load(run_dir)
    if state['status'] not in ('policy_checked', 'user_approved_exception'):
        raise ValueError('Нельзя сохранить ответ для пропущенного или непроверенного материала')
    text = Path(answer).read_text(encoding='utf-8-sig')
    if json_output:
        def visit(value):
            if isinstance(value, str):
                return restore_text(value, state['names'])
            if isinstance(value, list):
                return [visit(item) for item in value]
            if isinstance(value, dict):
                return {key: visit(item) for key, item in value.items()}
            return value
        restored = json.dumps(visit(json.loads(text)), ensure_ascii=False, indent=2)
    else:
        restored = restore_text(text, state['names'])
    with Path(output).open('x', encoding='utf-8', newline='') as stream:
        stream.write(restored)
    return {'status': 'restored'}
