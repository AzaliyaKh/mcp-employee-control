"""Real Bitrix material: reconstruct history, mask locally, send only checked evidence."""
from datetime import datetime, timedelta, time, date, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from analyzer.client import validate_findings
from analyzer.socket_client import analyze_socket
from employees.registry import Registry
from integrations.sources import Bitrix
from privacy.text import prepare_text, restore_text
from worklog.statistics import LOCAL_ZONE, statistics, timestamp


class HistoryError(ValueError):
    """Fixed diagnostic codes, never raw remote values or exception text."""
    MESSAGES = {
        'creation_missing': 'Нет события создания задачи в истории',
        'creation_inconsistent': 'Событие создания раньше даты задачи или не является началом истории',
        'creation_ambiguous': 'В истории несколько событий создания задачи',
        'history_value_invalid': 'В изменении истории отсутствуют from/to',
        'assignee_missing': 'В истории не указан исполнитель',
        'status_unknown': 'Не поддерживается формат статуса в истории',
        'history_gap': 'Значение from не совпадает с предыдущим to',
        'snapshot_mismatch': 'Последнее значение истории не совпадает с текущей задачей',
        'date_invalid': 'Не удалось прочитать дату в задаче или истории',
        'schema_invalid': 'Отсутствует обязательное поле или неверна структура истории',
    }

    def __init__(self, code, field=None):
        self.code = code
        self.field = field if field in ('RESPONSIBLE_ID', 'STATUS', 'DEADLINE', 'createdDate') else None
        message = self.MESSAGES[code]
        if self.field:
            message += ': ' + self.field
        super().__init__(message)


def field(task, name):
    snake = ''.join('_' + c if c.isupper() else c for c in name).upper()
    return task.get(name, task.get(snake))


def instant(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).isdigit():
        try:
            return datetime.fromtimestamp(int(value), timezone.utc).astimezone(LOCAL_ZONE)
        except (ValueError, OverflowError, OSError):
            raise ValueError('Некорректный timestamp Bitrix') from None
    if not isinstance(value, str):
        raise ValueError('Некорректная дата Bitrix')
    try:
        return timestamp(value)
    except ValueError:
        # Some self-hosted Bitrix installations use portal-local legacy dates.
        for pattern in ('%d.%m.%Y %H:%M:%S', '%d.%m.%Y %H:%M'):
            try:
                return datetime.strptime(value, pattern).replace(tzinfo=LOCAL_ZONE)
            except ValueError:
                pass
        raise ValueError('Неизвестный формат даты Bitrix') from None


def scalar(value):
    if isinstance(value, dict):
        value = value.get('id', value.get('ID'))
    if value is None or value == '':
        return None
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise HistoryError('schema_invalid')
    return str(value)


def normalize(raw, identities):
    try:
        return _normalize(raw, identities)
    except HistoryError:
        raise
    except (KeyError, TypeError, AttributeError):
        raise HistoryError('schema_invalid') from None
    except (ValueError, OverflowError):
        raise HistoryError('date_invalid') from None


def _normalize(raw, identities):
    task = raw['task']
    created = instant(field(task, 'createdDate'))
    history = sorted(raw['history'], key=lambda e: (instant(e['createdDate']), int(e.get('id', 0))))
    creation_events = [e for e in history if e.get('field') == 'NEW']
    if not creation_events:
        raise HistoryError('creation_missing', 'createdDate')
    if len(creation_events) != 1:
        raise HistoryError('creation_ambiguous', 'createdDate')
    known_from = instant(creation_events[0]['createdDate'])
    if known_from < created or history[0] is not creation_events[0]:
        raise HistoryError('creation_inconsistent', 'createdDate')
    changes = {'RESPONSIBLE_ID': [], 'STATUS': [], 'DEADLINE': []}
    use_real_status = not any(e.get('field') == 'STATUS' for e in history)
    for event in history:
        key = event.get('field')
        if key == 'REAL_STATUS' and use_real_status:
            key = 'STATUS'
        if key in changes:
            if not isinstance(event.get('value'), dict) or not {'from', 'to'} <= event['value'].keys():
                raise HistoryError('history_value_invalid', key)
            changes[key].append(event)
    current = {'RESPONSIBLE_ID': scalar(field(task, 'responsibleId')),
               'STATUS': scalar(field(task, 'status')),
               'DEADLINE': scalar(field(task, 'deadline'))}

    def converted(key, value):
        value = scalar(value)
        if key == 'RESPONSIBLE_ID':
            if value is None:
                raise HistoryError('assignee_missing', key)
            return identities.get(value, 'BITRIX_USER_' + value)
        if key == 'STATUS':
            if value not in ('1', '2', '3', '4', '5', '6', '7'):
                raise HistoryError('status_unknown', key)
            return 'completed' if value == '5' else ('in_progress' if value == '3' else 'open')
        return instant(value).astimezone(LOCAL_ZONE).isoformat() if value and value != '0' else None

    events = []
    for key, values in changes.items():
        initial = values[0]['value']['from'] if values else current[key]
        kind = {'RESPONSIBLE_ID': 'assignment', 'STATUS': 'status', 'DEADLINE': 'deadline'}[key]
        payload = 'employee' if kind == 'assignment' else 'value'
        events.append({'at': known_from.isoformat(), 'type': kind, payload: converted(key, initial)})
        previous = converted(key, initial)
        for event in values:
            if converted(key, event['value']['from']) != previous:
                raise HistoryError('history_gap', key)
            previous = converted(key, event['value']['to'])
            events.append({'at': instant(event['createdDate']).isoformat(), 'type': kind, payload: previous})
        if previous != converted(key, current[key]):
            raise HistoryError('snapshot_mismatch', key)
    # Stable chronological sorting preserves initial assignment before a completion.
    priority = {'assignment': 0, 'deadline': 1, 'status': 2}
    events.sort(key=lambda e: (instant(e['at']), priority[e['type']]))
    return {'key': 'bitrix:' + str(field(task, 'id')), 'created_at': created.isoformat(),
            'title': field(task, 'title') or f"Задача {field(task, 'id')}",
            'history_valid_from': known_from.isoformat(), 'history_complete': True, 'events': events}


def collect(config, request, registry_path, stage):
    cloud_review = request.get('kind', 'employee_review') == 'employee_review'
    settings = config['bitrix']
    base = os.environ.get(settings.get('base_env', 'BITRIX_WEBHOOK_URL'), '')
    if not base:
        raise ValueError('Не настроен BITRIX_WEBHOOK_URL')
    source = Bitrix(base)
    registry = Registry(registry_path, read_only=True)
    try:
        people = registry.list()
    finally:
        registry.close()
    person = request['employee']
    if not person.get('bitrix_id'):
        raise ValueError('У сотрудника не указан ID Bitrix')
    identities = {p['bitrix_id']: p['label'] for p in people if p.get('bitrix_id')}
    right = min(datetime.combine(date.fromisoformat(request['end']) + timedelta(days=1), time.min, LOCAL_ZONE),
                datetime.now(LOCAL_ZONE))
    stage('get_tasks', 'started')
    ids = source.tasks(right.isoformat(), settings.get('max_tasks', 1000))
    stage('get_tasks', 'succeeded')
    tasks, materials, skipped, sources = [], [], [], []
    for ident in ids:
        active = ['get_task']
        def observe(phase, status):
            active[0] = phase
            stage(phase, status, str(ident))
        try:
            raw = source.task(ident, observe, include_results=cloud_review)
            if scalar(field(raw['task'], 'id')) != str(ident):
                raise ValueError('Bitrix вернул другую задачу')
            observe('normalize', 'started')
            normalized = normalize(raw, identities)
            stage('normalize', 'succeeded', str(ident))
            one = statistics([normalized], person['label'], request['start'], request['end'])
            tasks.append(normalized)
            if not one['assigned']:
                stage('filter', 'succeeded', str(ident))
                continue
            task = raw['task']
            sources.append({'id': 'bitrix:' + str(ident), 'task_id': ident,
                            'task_title': field(task, 'title') or f'Задача {ident}',
                            'url': f"{urlsplit(base).scheme}://{urlsplit(base).netloc}/company/personal/user/{person['bitrix_id']}/tasks/task/view/{ident}/"})
            if not cloud_review:
                continue
            # Current title/description are not reliable historical evidence after later edits.
            changed = field(task, 'changedDate')
            historical_text_unknown = not changed or instant(changed) >= right
            text = {'employee': person['full_name'], 'task_id': ident,
                    'title': '' if historical_text_unknown else (field(task, 'title') or ''),
                    'description': '' if historical_text_unknown else (field(task, 'description') or ''),
                    'counts': one['counts'], 'reports': [],
                    'limitations': ['Только Bitrix; код и файлы отчётов не получены.']}
            if historical_text_unknown:
                text['limitations'].append('Текст изменён после среза; поздняя версия исключена.')
            for result in raw['results']:
                if not isinstance(result, dict):
                    raise ValueError('Некорректный результат задачи')
                at = result.get('createdAt', result.get('createdDate'))
                if not at:
                    text['limitations'].append('Результат без даты исключён из исторического анализа.')
                    continue
                if instant(at) >= right:
                    continue
                modified = result.get('updatedAt', result.get('modifiedAt'))
                if modified and instant(modified) >= right:
                    text['limitations'].append('Поздняя редакция результата исключена.')
                    continue
                text['reports'].append({'text': result.get('text', ''), 'date': at})
                if result.get('files') or result.get('fileIds'):
                    text['limitations'].append('Вложение не прочитано; сравнение с файлом не выполнено.')
            observe('mask', 'started')
            candidate, names, findings = prepare_text(json.dumps(text, ensure_ascii=False), people)
            if len(candidate) > 200000:
                raise ValueError('Материал задачи превышает предел анализатора')
            materials.append({'task_id': ident, 'task_title': field(task, 'title') or f'Задача {ident}',
                              'source_id': 'bitrix:' + str(ident),
                              'text': candidate, 'names': names, 'findings': findings,
                              'version': hashlib.sha256(candidate.encode()).hexdigest(),
                              'status': 'awaiting_privacy_decision' if findings else 'policy_checked'})
            stage('mask', 'succeeded', str(ident))
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            # Exception messages from remote responses never reach logs or the model.
            reason = str(error) if isinstance(error, HistoryError) else 'Не удалось получить или подтвердить историю/результаты задачи'
            skipped.append({'task_id': ident, 'stage': active[0], 'reason': reason,
                            'reason_code': error.code if isinstance(error, HistoryError) else 'task_data_invalid'})
            # New adapters accept structured details; keep the original observer signature for callers.
            if hasattr(stage, 'failure'):
                stage.failure(active[0], str(ident), reason,
                              error.code if isinstance(error, HistoryError) else 'task_data_invalid')
            else:
                stage(active[0], 'failed', str(ident))
    stage('statistics', 'started')
    report = statistics(tasks, person['label'], request['start'], request['end'])
    stage('statistics', 'succeeded')
    report.update(employee_name=person['full_name'], source_mode='bitrix', demo=False,
                  analysis_kind='bitrix_statistics_with_codex' if cloud_review else 'local_statistics', skipped=skipped,
                  sources=sources,
                  limitations=['Учтены задачи, видимые владельцу вебхука. Удалённые и недоступные задачи получить нельзя.',
                               'Сравнение отчёта с кодом GitLab в этом режиме не выполняется.'])
    report['partial'] = bool(report['partial'] or skipped)
    return {'report': report, 'materials': materials}


def analyze_materials(state, config, stage, checkpoint=None):
    report = state['report']
    analyses = report.setdefault('analyses', [])
    analyzed = {a['task_id'] for a in analyses}
    for material in state['materials']:
        ident = str(material['task_id'])
        if material['status'] == 'skipped':
            report['partial'] = True
            if not any(s['task_id'] == material['task_id'] for s in report['skipped']):
                report['skipped'].append({'task_id': material['task_id'],
                    'task_title': material.get('task_title', f"Задача {material['task_id']}"),
                    'reason': 'Задача не рассматривалась в облачном анализе: пользователь отказался от отправки данных'})
            continue
        if material['status'] == 'awaiting_privacy_decision' or material['task_id'] in analyzed:
            continue
        if material['status'] not in ('policy_checked', 'user_approved_exception'):
            raise ValueError('Отправка непроверенного материала запрещена')
        text = material['text']
        bundle = {'protocol': 1, 'status': material['status'], 'text': text,
                  'version': material['version'], 'employee_labels': list(material['names']),
                  'source_ids': [material['source_id']]}
        stage('codex', 'started', ident)
        answer = validate_findings(analyze_socket(bundle, config['analyzer']), bundle)
        stage('codex', 'succeeded', ident)
        stage('restore', 'started', ident)
        def restore(value):
            if isinstance(value, str):
                return restore_text(value, material['names'])
            if isinstance(value, list):
                return [restore(v) for v in value]
            if isinstance(value, dict):
                return {k: restore(v) for k, v in value.items()}
            return value
        analyses.append({'task_id': material['task_id'], **restore(answer)})
        stage('restore', 'succeeded', ident)
        if checkpoint:
            checkpoint()
    report['analyses'] = analyses
    report['analysis_summary'] = '\n'.join(f"Задача {a['task_id']}: {a['summary']}" for a in analyses)
    return report
