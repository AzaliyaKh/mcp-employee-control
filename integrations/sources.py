import re
from urllib.parse import quote

from worklog.statistics import timestamp
from .transport import JsonTransport


class Bitrix:
    def __init__(self, base_url):
        self.http = JsonTransport(base_url)

    def _call(self, method, params):
        result, _ = self.http.request(method, params)
        if not isinstance(result, dict) or 'error' in result or 'result' not in result:
            raise ValueError('Bitrix не вернул ожидаемый результат')
        return result

    def _list(self, method, task_id):
        items, seen, start = [], set(), 0
        for _ in range(100):
            response = self._call(method, {'taskId': task_id, 'start': start})
            values = response['result']
            if isinstance(values, dict):
                values = values.get('list', values.get('items'))
            if not isinstance(values, list):
                raise ValueError('Неизвестный формат списка Bitrix')
            items.extend(values)
            following = response.get('next')
            if following is None:
                return items
            if not isinstance(following, int) or following in seen or following <= start:
                raise ValueError('Некорректная пагинация Bitrix')
            seen.add(following)
            start = following
        raise ValueError('Превышен предел страниц Bitrix; выгрузка не считается полной')

    def task(self, task_id, observer=None, include_results=True):
        def observed(stage, operation):
            if observer:
                observer(stage, 'started')
            try:
                value = operation()
            except (ValueError, KeyError, TypeError):
                if observer:
                    observer(stage, 'failed')
                raise
            if observer:
                observer(stage, 'succeeded')
            return value
        response = observed('get_task', lambda: self._call('tasks.task.get', {'taskId': task_id}))
        task = response['result']['task']
        results = observed('get_reports', lambda: self._list('tasks.task.result.list', task_id)) if include_results else []
        history = observed('get_history', lambda: self._list('tasks.task.history.list', task_id))
        relevant = [event for event in history if event.get('field') in (
            'NEW', 'RESPONSIBLE_ID', 'STATUS', 'REAL_STATUS', 'DEADLINE')]
        return {'source': 'bitrix', 'task': task, 'results': results,
                'history': relevant, 'history_complete': False,
                'limitations': ['Сырая выгрузка: полнота истории и исходное состояние ещё не проверены',
                                'Вложения пока представлены идентификаторами, не скачаны']}

    def tasks(self, until, limit=1000):
        """All visible tasks, including reassigned ones; never filter by current assignee."""
        items, seen, start = [], set(), 0
        for _ in range(1000):
            response = self._call('tasks.task.list', {'filter': {'<CREATED_DATE': until},
                'select': ['ID'], 'order': {'ID': 'ASC'}, 'start': start})
            values = response['result']
            if isinstance(values, dict):
                values = values.get('tasks', values.get('items'))
            if not isinstance(values, list):
                raise ValueError('Неизвестный формат списка задач Bitrix')
            for value in values:
                ident = str(value.get('id', value.get('ID', ''))) if isinstance(value, dict) else ''
                if not ident.isdigit() or ident in seen:
                    raise ValueError('Некорректные или повторные задачи Bitrix')
                seen.add(ident)
                items.append(int(ident))
                if len(items) > limit:
                    raise ValueError('Превышен предел задач; список не считается полным')
            following = response.get('next')
            if following is None:
                return items
            if not isinstance(following, int) or following <= start:
                raise ValueError('Некорректная пагинация Bitrix')
            start = following
        raise ValueError('Превышен предел страниц Bitrix')


class Kaiten:
    def __init__(self, base_url, token, report_property_id=None):
        self.http = JsonTransport(base_url, {'Authorization': 'Bearer ' + token})
        self.report_property_id = report_property_id

    def card(self, card_id):
        card, _ = self.http.request(f'cards/{card_id}')
        if not isinstance(card, dict) or card.get('id') != card_id:
            raise ValueError('Kaiten не вернул ожидаемую карточку')
        report = None
        status = 'not_configured'
        if self.report_property_id is not None:
            properties = card.get('properties')
            if properties is not None and not isinstance(properties, dict):
                raise ValueError('Kaiten: properties must be an object or null')
            report = (properties or {}).get(f'id_{self.report_property_id}')
            status = 'available' if isinstance(report, str) and report.strip() else 'absent'
            if report is not None and not isinstance(report, str):
                status = 'unsupported'
        return {'source': 'kaiten', 'card': card, 'report': report,
                'report_status': status, 'history_complete': False,
                'limitations': ['Источник итогового отчёта задаётся разработчиком; чат не просматривается',
                                'Получена карточка, а не полная история назначений и сроков']}


class GitLab:
    def __init__(self, base_url, token):
        self.http = JsonTransport(base_url, {'PRIVATE-TOKEN': token})

    def _pages(self, path, params=None):
        values, page = [], 1
        for _ in range(100):
            response, headers = self.http.request(path, params={**(params or {}),
                                                               'page': page, 'per_page': 100})
            if not isinstance(response, list):
                raise ValueError('Неизвестный формат списка GitLab')
            values.extend(response)
            following = next((value for key, value in headers.items()
                               if key.lower() == 'x-next-page'), None)
            if following is None:
                if len(response) < 100:
                    return values
                following = str(page + 1)
            if following == '':
                return values
            if not str(following).isdigit() or int(following) <= page:
                raise ValueError('Некорректная пагинация GitLab')
            page = int(following)
        raise ValueError('Превышен предел страниц GitLab; выгрузка не считается полной')

    def changes(self, project, person, since, until):
        left, right = timestamp(since), timestamp(until)
        if left >= right:
            raise ValueError('Неверный период коммитов')
        emails = {value.casefold() for value in person['gitlab_emails']
                  + person.get('history', {}).get('gitlab_emails', [])}
        if not emails:
            raise ValueError('В справочнике отсутствуют почты автора коммитов GitLab')
        if not project or any(part in ('', '.', '..') for part in project.split('/')):
            raise ValueError('Неверный путь проекта GitLab')
        path = 'projects/' + quote(project, safe='') + '/repository/commits'
        commits = self._pages(path, {'since': since, 'until': until, 'all': 'true'})
        selected, seen, partial = [], set(), False
        for commit in commits:
            if not isinstance(commit, dict) or not isinstance(commit.get('author_email'), str):
                raise ValueError('GitLab: invalid commit object')
            if commit.get('author_email', '').casefold() not in emails:
                continue
            if not left <= timestamp(commit['committed_date']) < right:
                continue
            sha = commit['id']
            if not re.fullmatch(r'[0-9a-fA-F]{40,64}', sha):
                raise ValueError('GitLab вернул некорректный идентификатор коммита')
            if sha in seen:
                continue
            seen.add(sha)
            changes = self._pages(path + '/' + sha + '/diff')
            if not all(isinstance(item, dict) for item in changes):
                raise ValueError('GitLab: invalid diff object')
            if any(item.get('too_large') or item.get('collapsed') for item in changes):
                partial = True
            selected.append({**commit, 'changes': changes})
        return {'source': 'gitlab', 'project': project, 'employee': person['label'],
                'commits': selected, 'partial': partial,
                'limitations': ['Лимиты GitLab могут исключать изменения; полнота больших diff требует отдельной проверки']}
