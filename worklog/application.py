"""Persistent local jobs; normalized files are an explicit offline source."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid

from employees.registry import Registry
from .statistics import statistics


def checked_id(value):
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('Неверный идентификатор запуска/отчёта')
    return value


class Application:
    def __init__(self, data_dir, config_path=None):
        self.root = Path(data_dir).resolve()
        self.config_path = Path(config_path).resolve() if config_path else None
        private = self.root / 'private'
        private.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(private / 'runs.sqlite', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL,
            created_at TEXT NOT NULL, report_id TEXT, stage TEXT NOT NULL, error TEXT)''')
        if 'request_key' not in {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN request_key TEXT')
        self.db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS active_requests ON jobs(request_key)
            WHERE status IN ('queued', 'running')''')
        self.db.commit()

    def close(self):
        self.db.close()

    def recover_interrupted(self):
        """Called once at server startup; never automatically retry an unknown outcome."""
        rows = self.db.execute("SELECT id,stage FROM jobs WHERE status='running'").fetchall()
        for row in rows:
            with self.db:
                self.db.execute("UPDATE jobs SET status='interrupted',error=? WHERE id=?",
                                ('Сервис был остановлен во время обработки; автоматический повтор не выполнен', row['id']))
            self._log(row['id'], row['stage'], False, 'Обработка прервана остановкой сервиса')
            folder = self.root / 'work' / checked_id(row['id'])
            if folder.exists():
                if folder.is_symlink() or folder.resolve().parent != (self.root / 'work').resolve():
                    raise ValueError('Небезопасный путь временных файлов')
                shutil.rmtree(folder)
        return len(rows)

    def resolve_employee(self, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError('Укажите имя или метку сотрудника')
        registry = Registry(self.root / 'private' / 'registry.sqlite', read_only=True)
        try:
            candidates = []
            for person in registry.list():
                searchable = [person['label'], person['full_name'], *person['aliases'],
                              *person.get('history', {}).get('full_name', [])]
                if any(query.strip().casefold() in value.casefold() for value in searchable):
                    candidates.append({'employee_id': person['label'], 'name': person['full_name']})
            return {'candidates': candidates}
        finally:
            registry.close()

    def start(self, employee, start, end, kind='employee_statistics'):
        if kind not in ('employee_statistics', 'employee_review'):
            raise ValueError('Неизвестный вид запроса')
        statistics([], employee, start, end)
        registry = Registry(self.root / 'private' / 'registry.sqlite', read_only=True)
        try:
            person = registry.get(employee)
        finally:
            registry.close()
        if self.config_path is None:
            raise ValueError('Разработчик должен указать файл конфигурации')
        config = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
        request = {'employee': person, 'start': start, 'end': end, 'kind': kind,
                   'demo': config.get('demo') is True}
        if config.get('source') == 'bitrix':
            if not person.get('bitrix_id'):
                raise ValueError('У сотрудника не указан ID Bitrix')
            if kind == 'employee_review' and config.get('analyzer', {}).get('kind') != 'codex_socket':
                raise ValueError('Не настроен изолированный Codex')
            request['pipeline_config'] = config
        else:
            if kind == 'employee_review':
                raise ValueError('Облачный анализ требует источника Bitrix')
            source = Path(config['source_file'])
            if not source.is_absolute():
                source = self.config_path.parent / source
            request['source_file'] = str(source.resolve())
        ident = uuid.uuid4().hex
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
        request_key = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            active = self.db.execute("SELECT id FROM jobs WHERE request_key = ? AND status IN ('queued', 'running', 'awaiting_privacy_decision')",
                                     (request_key,)).fetchone()
            if active:
                ident = active['id']
            else:
                self.db.execute('''INSERT INTO jobs (id,status,request,created_at,stage,request_key)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                    (ident, 'queued', encoded, datetime.now(timezone.utc).isoformat(), 'queue', request_key))
        return self.status(ident)

    def _row(self, ident):
        row = self.db.execute('SELECT * FROM jobs WHERE id = ?', (checked_id(ident),)).fetchone()
        if row is None:
            raise ValueError('Запуск не найден')
        return row

    def status(self, ident):
        row = self._row(ident)
        result = {'run_id': row['id'], 'status': row['status'], 'stage': row['stage'],
                  'report_id': row['report_id'], 'error': row['error']}
        if row['status'] == 'awaiting_privacy_decision':
            state = json.loads((self.root / 'work' / ident / 'pipeline.json').read_text(encoding='utf-8'))
            pending = [m for m in state['materials'] if m['status'] == 'awaiting_privacy_decision']
            result['pending_privacy_count'] = len(pending)
            result['skipped_task_ids'] = [m['task_id'] for m in state['materials'] if m['status'] == 'skipped']
            result['checked_task_ids'] = [m['task_id'] for m in state['materials']
                                          if m['status'] in ('policy_checked', 'user_approved_exception')]
            result['analyzed_task_ids'] = [a['task_id'] for a in state['report'].get('analyses', [])]
            result['progress'] = {'counts': state['report']['counts'],
                                  'analysis_summary': '\n'.join(a['summary'] for a in state['report'].get('analyses', []))[:8000]}
            result['privacy'] = [{**{k: m[k] for k in ('task_id', 'version', 'findings')},
                                  'task_title': m.get('task_title', f"Задача {m['task_id']}"),
                                  'question': f"В задаче «{m.get('task_title', 'Задача ' + str(m['task_id']))}» остались персональные данные. Отправить эту задачу в облачный ИИ? «Нет» — исключить только эту задачу из анализа, «Продолжить» — отправить показанные данные как есть.",
                                  'fragment': m['text'][max(0, m['findings'][0]['start'] - 80):m['findings'][0]['end'] + 80]}
                                 for m in pending[:1]]
        if row['report_id'] and row['status'] in ('succeeded', 'partial'):
            report = self.report(row['report_id'])
            result['summary'] = {key: report[key] for key in ('employee_name', 'counts', 'partial')}
            result['summary']['demo'] = report.get('demo', False)
            result['summary']['source_mode'] = report.get('source_mode', 'normalized_file')
            sources = {source['id']: source for source in report.get('sources', [])}
            for category in ('completed', 'current'):
                details = report.get(category + '_tasks', [{'key': key, 'title': sources.get(key, {}).get('task_title', key)}
                                                          for key in report.get(category, [])])
                result['summary'][category + '_tasks'] = [
                    {**item, **({'url': sources[item['key']]['url']} if sources.get(item['key'], {}).get('url') else {})}
                    for item in details]
            for key in ('analysis_summary', 'skipped', 'limitations'):
                if key in report:
                    result['summary'][key] = report[key]
            if len(result['summary'].get('analysis_summary', '')) > 8000:
                result['summary']['analysis_summary'] = result['summary']['analysis_summary'][:8000] + '\nПолное пояснение — в отчёте.'
            if len(result['summary'].get('skipped', [])) > 50:
                result['summary']['skipped_count'] = len(result['summary']['skipped'])
                result['summary']['skipped'] = result['summary']['skipped'][:50]
        return result

    def decide_privacy(self, ident, task_id, version, action, cleaned_text=None):
        if action not in ('continue', 'skip', 'clean'):
            raise ValueError('Неизвестное действие')
        path = self.root / 'work' / checked_id(ident) / 'pipeline.json'
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self._row(ident)['status'] != 'awaiting_privacy_decision':
                raise ValueError('Запрос не ожидает решения')
            state = json.loads(path.read_text(encoding='utf-8'))
            material = next((m for m in state['materials'] if m['task_id'] == task_id), None)
            if material is None or material['status'] != 'awaiting_privacy_decision' or material['version'] != version:
                raise ValueError('Неверная задача или версия')
            pending = next(m for m in state['materials'] if m['status'] == 'awaiting_privacy_decision')
            if pending['task_id'] != task_id:
                raise ValueError('Сначала требуется отдельное решение по показанной задаче')
            if action == 'clean':
                if not isinstance(cleaned_text, str) or len(cleaned_text) > 1000:
                    raise ValueError('Требуется исправленный текст')
                finding = material['findings'][0]
                cleaned_text = material['text'][:finding['start']] + cleaned_text + material['text'][finding['end']:]
                from privacy.text import prepare_text
                registry = Registry(self.root / 'private' / 'registry.sqlite', read_only=True)
                try:
                    # Existing valid EMP labels are protected from being treated as literal source text.
                    import re
                    protected = re.sub(r'(?<!\w)EMP_[0-9]+(?!\w)',
                                       lambda m: f"MASKEDLABEL{m[0][4:]}END", cleaned_text)
                    candidate, names, findings = prepare_text(protected, registry.list())
                    candidate = re.sub(r'MASKEDLABEL([0-9]+)END', r'EMP_\1', candidate)
                    known = set(material['names']) | set(names)
                    if not set(re.findall(r'(?<!\w)EMP_[0-9]+(?!\w)', candidate)) <= known:
                        raise ValueError('Неизвестная метка в очищенном тексте')
                finally:
                    registry.close()
                material.update(text=candidate, version=hashlib.sha256(candidate.encode()).hexdigest(),
                                findings=findings, names={**material['names'], **names},
                                status='awaiting_privacy_decision' if findings else 'policy_checked')
            else:
                material['status'] = 'user_approved_exception' if action == 'continue' else 'skipped'
            material['decision'] = {'action': action, 'shown_version': version,
                                    'time': datetime.now(timezone.utc).isoformat()}
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
            analyzed = {a['task_id'] for a in state['report'].get('analyses', [])}
            ready = any(m['status'] in ('policy_checked', 'user_approved_exception') and m['task_id'] not in analyzed
                        for m in state['materials'])
            if ready or not any(m['status'] == 'awaiting_privacy_decision' for m in state['materials']):
                self.db.execute("UPDATE jobs SET status='queued',stage='privacy_checked' WHERE id=?", (ident,))
        self._log_privacy_decision(ident, task_id, action)
        result = self.status(ident)
        result['decision_applied'] = {'task_id': task_id, 'action': action,
                                      'scope': 'single_task', 'shown_version': version}
        return result

    def _log_privacy_decision(self, ident, task_id, action):
        event = {'time': datetime.now(timezone.utc).isoformat(), 'run_id': ident,
                 'stage': 'privacy_decision', 'status': 'succeeded', 'task_id': task_id,
                 'action': action, 'scope': 'single_task', 'message': 'Решение применено только к указанной задаче'}
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        encoded = json.dumps(event, ensure_ascii=False)
        with (logs / f'{checked_id(ident)}.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(encoded + '\n')
        print(encoded, flush=True)

    def _stage(self, ident, stage):
        with self.db:
            self.db.execute('UPDATE jobs SET stage = ? WHERE id = ?', (stage, ident))

    def _log(self, ident, stage, success=True, reason=None):
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        with (logs / f'{checked_id(ident)}.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'run_id': ident,
                                     'stage': stage, 'message': (
                'этап завершился успешно' if success else f'этап остановлен: {reason}')},
                ensure_ascii=False) + '\n')

    def _pipeline_stage(self, ident, stage, status, task_id=None, reason=None, reason_code=None):
        self._stage(ident, stage)
        event = {'time': datetime.now(timezone.utc).isoformat(), 'run_id': ident,
                 'stage': stage, 'status': status, 'task_id': task_id,
                 'message': {'started': 'этап начался', 'succeeded': 'этап завершился успешно',
                             'waiting': 'этап остановлен: ожидается решение пользователя по персональным данным',
                             'failed': 'этап остановлен: данные задачи недоступны или некорректны'}[status]}
        if reason is not None:
            event['message'] = 'этап остановлен: ' + reason
            event['reason_code'] = reason_code
        logs = self.root / 'logs'
        logs.mkdir(exist_ok=True)
        encoded = json.dumps(event, ensure_ascii=False)
        with (logs / f'{checked_id(ident)}.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(encoded + '\n')
        print(encoded, flush=True)

    def _process_bitrix(self, ident, request, work):
        from .bitrix_pipeline import collect, analyze_materials
        config = request['pipeline_config']
        path = work / 'pipeline.json'
        emit = lambda stage, status, task_id=None: self._pipeline_stage(ident, stage, status, task_id)
        emit.failure = lambda stage, task_id, reason, code: self._pipeline_stage(
            ident, stage, 'failed', task_id, reason, code)
        if path.exists():
            state = json.loads(path.read_text(encoding='utf-8'))
        else:
            state = collect(config, request, self.root / 'private' / 'registry.sqlite', emit)
            path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        cloud_review = request.get('kind', 'employee_review') == 'employee_review'
        if cloud_review:
            emit('privacy', 'started')
        def checkpoint():
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
            temporary.replace(path)
        # Only checked tasks reach Codex; blocked tasks remain local and do not hold up the rest.
        report = analyze_materials(state, config, emit, checkpoint=checkpoint) if cloud_review else state['report']
        checkpoint()
        if cloud_review and any(m['status'] == 'awaiting_privacy_decision' for m in state['materials']):
            with self.db:
                self.db.execute("UPDATE jobs SET status='awaiting_privacy_decision',stage='privacy' WHERE id=?", (ident,))
            emit('privacy', 'waiting')
            return
        if cloud_review:
            emit('privacy', 'succeeded')
        emit('save_report', 'started')
        folder = self.root / 'reports' / ident
        folder.mkdir(parents=True, exist_ok=False)
        (folder / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        with self.db:
            self.db.execute('UPDATE jobs SET status=?,report_id=? WHERE id=?',
                            ('partial' if report['partial'] else 'succeeded', ident, ident))
        emit('save_report', 'succeeded')

    def process(self, ident):
        row = self._row(ident)
        with self.db:
            changed = self.db.execute("UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
                                      (ident,)).rowcount
        if not changed:
            return self.status(ident)
        request = json.loads(row['request'])
        work = self.root / 'work' / ident
        stage = 'collect'
        try:
            work.mkdir(parents=True, exist_ok='pipeline_config' in request)
            if 'pipeline_config' in request:
                self._process_bitrix(ident, request, work)
                return self.status(ident)
            self._stage(ident, stage)
            source = Path(request['source_file']).read_text(encoding='utf-8-sig')
            (work / 'tasks.json').write_text(source, encoding='utf-8')
            tasks = json.loads(source)
            self._log(ident, stage)
            stage = 'statistics'
            self._stage(ident, stage)
            report = statistics(tasks, request['employee']['label'], request['start'], request['end'])
            report['employee_name'] = request['employee']['full_name']
            report['analysis_kind'] = 'local_statistics'
            report['source_mode'] = 'normalized_file'
            report['demo'] = request.get('demo', False)
            self._log(ident, stage)
            stage = 'save_report'
            self._stage(ident, stage)
            report_dir = self.root / 'reports' / ident
            report_dir.mkdir(parents=True, exist_ok=False)
            with (report_dir / 'report.json').open('x', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
            with self.db:
                self.db.execute('UPDATE jobs SET status = ?, report_id = ? WHERE id = ?',
                                ('partial' if report['partial'] else 'succeeded', ident, ident))
            self._log(ident, stage)
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
            if 'pipeline_config' in request:
                stage = self._row(ident)['stage']
                reason = {'get_tasks': 'Не удалось получить список задач Bitrix: проверьте вебхук, права и лимиты',
                          'codex': 'Codex недоступен или вернул ошибку: проверьте вход, модель и время ожидания',
                          'restore': 'Не удалось восстановить имена в ответе Codex'}.get(stage,
                              'Ошибка обработки Bitrix: проверьте настройки и структуру данных')
            elif isinstance(error, FileNotFoundError):
                reason = 'Файл источника не найден' if stage == 'collect' else 'Не найден необходимый файл'
            elif isinstance(error, PermissionError):
                reason = 'Недостаточно прав для чтения или сохранения файла'
            elif isinstance(error, json.JSONDecodeError):
                reason = 'Некорректный JSON источника'
            elif isinstance(error, (ValueError, KeyError, TypeError)):
                reason = 'Некорректная структура или значения истории задач'
            else:
                reason = f'{type(error).__name__}: ошибка локального хранилища'
            # Never put raw source text, URLs or credentials into the developer log.
            with self.db:
                self.db.execute("UPDATE jobs SET status = 'failed', error = ? WHERE id = ?",
                                (reason, ident))
            self._log(ident, stage, False, reason)
        finally:
            if work.exists() and self._row(ident)['status'] != 'awaiting_privacy_decision':
                if work.is_symlink() or work.resolve().parent != (self.root / 'work').resolve():
                    raise ValueError('Небезопасный путь очистки временных файлов')
                shutil.rmtree(work)
        return self.status(ident)

    def report(self, ident):
        row = self._row(ident)
        if row['status'] not in ('succeeded', 'partial') or row['report_id'] != ident:
            raise ValueError('Отчёт ещё не готов')
        return json.loads((self.root / 'reports' / checked_id(ident) / 'report.json').read_text(encoding='utf-8'))
