import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from analyzer.cli_worker import run_cli
from employees.registry import Registry
from worklog.application import Application
from worklog.bitrix_pipeline import normalize


def event(ident, when, field, before=None, after=None):
    return {'id': ident, 'createdDate': when, 'field': field, 'value': {'from': before, 'to': after}}


def fixture():
    return {'task': {'id': '1782', 'createdDate': '2026-10-01T09:00:00+05:00',
                     'changedDate': '2026-10-05T12:00:00+05:00', 'title': 'Иванов Иван: проверка',
                     'description': 'Иванов Иван исправил экспорт', 'responsibleId': '122',
                     'deadline': '2026-10-04T18:00:00+05:00', 'status': '5'},
            'history': [event(1, '2026-10-01T09:00:00+05:00', 'NEW'),
                        event(2, '2026-10-02T10:00:00+05:00', 'STATUS', '2', '3'),
                        event(3, '2026-10-05T12:00:00+05:00', 'STATUS', '3', '5')],
            'results': [{'text': 'Иванов Иван исправил экспорт', 'createdAt': '2026-10-05T12:00:00+05:00'}]}


class BitrixLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.raw = fixture()
        self.calls = []
        self.received = []
        self.reject_list = False
        self.page_two = False
        self.safe_second = False
        self.extra = False
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                method = self.path.rsplit('/', 1)[-1]
                owner.calls.append((method, body))
                if method == 'tasks.task.list':
                    response = {'result': {'tasks': [{'id': '1782'}]}}
                    if owner.reject_list:
                        response = {'error': 'ACCESS_DENIED', 'error_description': 'SECRET-SOURCE-CONTENT'}
                    elif owner.page_two and body['start'] == 0:
                        response['next'] = 50
                    elif owner.page_two:
                        response = {'result': {'tasks': [{'id': '1783'}]}}
                elif method == 'tasks.task.get':
                    task = copy.deepcopy(owner.raw['task'])
                    if body['taskId'] == 1783:
                        task['id'] = '1783'
                        if owner.safe_second:
                            task['description'] = 'Проверенный текст'
                    response = {'result': {'task': task}}
                elif method == 'tasks.task.history.list':
                    response = {'result': {'list': owner.raw['history']}}
                elif method == 'tasks.task.result.list':
                    response = {'result': owner.raw['results']}
                else:
                    response = {'error': 'UNKNOWN_METHOD'}
                encoded = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)
        env = patch.dict(os.environ, {'BITRIX_WEBHOOK_URL': f'http://127.0.0.1:{server.server_port}/rest/1/SECRET/'})
        env.start()
        self.addCleanup(env.stop)
        registry = Registry(self.root / 'private/registry.sqlite')
        try:
            self.person = registry.add({'full_name': 'Иванов Иван', 'aliases': [], 'bitrix_id': '122',
                'kaiten_id': '', 'gitlab_username': '', 'gitlab_emails': [], 'emails': [], 'phones': [], 'logins': []})
        finally:
            registry.close()
        config = {'source': 'bitrix', 'bitrix': {'max_tasks': 1000},
                  'analyzer': {'kind': 'codex_socket', 'socket': '/synthetic/codex.sock'}}
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.app = Application(self.root, self.config)
        self.addCleanup(self.app.close)

    def start(self):
        return self.app.start('EMP_001', '2026-10-01', '2026-10-31', kind='employee_review')['run_id']

    def analyze(self, bundle, config):
        self.received.append(bundle)
        self.assertNotIn('Иванов', bundle['text'])
        self.assertNotIn('SECRET', json.dumps(bundle))
        self.assertNotIn('names', bundle)
        return {'summary': 'EMP_001 выполнил задачу; качество кода не проверено.',
                'findings': [{'claim': 'Есть текстовый результат', 'status': 'confirmed',
                              'sources': [bundle['source_ids'][0]]}]}

    def process(self, ident):
        with patch('worklog.bitrix_pipeline.analyze_socket', side_effect=self.analyze):
            return self.app.process(ident)

    def test_real_http_bitrix_to_masked_analyzer_and_restored_report(self):
        ident = self.start()
        result = self.process(ident)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['summary']['counts'], {'assigned': 1, 'completed': 1, 'overdue': 1, 'current': 0})
        self.assertFalse(result['summary']['demo'])
        self.assertIn('Иванов Иван', result['summary']['analysis_summary'])
        self.assertEqual(len(self.received), 1)
        self.assertFalse((self.root / 'work' / ident).exists())
        report = self.app.report(ident)
        self.assertEqual(report['source_mode'], 'bitrix')
        events = [json.loads(s) for s in (self.root / 'logs' / f'{ident}.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(all('time' in e and 'run_id' in e for e in events))
        self.assertIn('codex', [e['stage'] for e in events])
        self.assertTrue({'get_reports', 'get_history', 'mask', 'normalize', 'statistics'} <= {e['stage'] for e in events})
        self.assertNotIn('SECRET', json.dumps(events))
        self.assertNotIn('Иванов', json.dumps(events, ensure_ascii=False))

    def test_statistics_is_local_without_masking_cloud_or_report_text_requests(self):
        self.raw['task']['description'] = 'unknown@example.test'
        config = json.loads(self.config.read_text(encoding='utf-8'))
        config.pop('analyzer')
        self.config.write_text(json.dumps(config), encoding='utf-8')
        with patch('worklog.bitrix_pipeline.prepare_text') as mask, \
                patch('worklog.bitrix_pipeline.analyze_socket') as cloud:
            ident = self.app.start('EMP_001', '2026-10-01', '2026-10-31')['run_id']
            result = self.app.process(ident)
        mask.assert_not_called()
        cloud.assert_not_called()
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('privacy', result)
        self.assertEqual(result['summary']['counts']['completed'], 1)
        self.assertEqual(self.app.report(ident)['analysis_kind'], 'local_statistics')
        self.assertNotIn('tasks.task.result.list', [method for method, _ in self.calls])
        events = [json.loads(s) for s in (self.root / 'logs' / f'{ident}.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue({'mask', 'privacy', 'codex'}.isdisjoint(e['stage'] for e in events))

    def test_statistics_and_cloud_review_are_distinct_requests(self):
        local = self.app.start('EMP_001', '2026-10-01', '2026-10-31')['run_id']
        review = self.start()
        self.assertNotEqual(local, review)

    def test_local_statistics_returns_titles_by_category_and_only_overdue_count(self):
        first = self.app.start('EMP_001', '2026-10-01', '2026-10-31')['run_id']
        result = self.app.process(first)['summary']
        self.assertEqual(result['completed_tasks'][0]['title'], self.raw['task']['title'])
        self.assertEqual(result['completed_tasks'][0]['key'], 'bitrix:1782')
        self.assertIn('/tasks/task/view/1782/', result['completed_tasks'][0]['url'])
        self.assertEqual(result['current_tasks'], [])
        self.assertEqual(result['counts']['overdue'], 1)
        self.assertNotIn('overdue_tasks', result)
        self.raw['task']['status'] = '3'
        self.raw['history'][-1]['value']['to'] = '3'
        second = self.app.start('EMP_001', '2026-10-01', '2026-10-31')['run_id']
        result = self.app.process(second)['summary']
        self.assertEqual(result['completed_tasks'], [])
        self.assertEqual(result['current_tasks'][0]['title'], self.raw['task']['title'])
        self.assertEqual(result['counts']['overdue'], 1)
        self.assertNotIn('overdue_tasks', result)

    def test_cloud_question_identifies_task_by_title_and_refusal_is_in_report(self):
        ident, material = self.blocked()
        self.assertEqual(material['task_title'], self.raw['task']['title'])
        self.assertIn(self.raw['task']['title'], material['question'])
        self.assertIn('«Нет»', material['question'])
        self.app.decide_privacy(ident, material['task_id'], material['version'], 'skip')
        result = self.process(ident)
        self.assertEqual(result['status'], 'partial')
        skipped = self.app.report(ident)['skipped']
        self.assertEqual(skipped[0]['task_title'], self.raw['task']['title'])
        self.assertIn('пользователь отказался', skipped[0]['reason'])
        self.assertEqual(self.received, [])

    def test_pagination_and_current_assignee_is_not_used_as_a_filter(self):
        self.page_two = True
        ident = self.start()
        result = self.process(ident)
        self.assertEqual(result['summary']['counts']['assigned'], 2)
        requests = [b for m, b in self.calls if m == 'tasks.task.list']
        self.assertEqual([b['start'] for b in requests], [0, 50])
        self.assertNotIn('RESPONSIBLE_ID', json.dumps(requests))

    def test_missing_creation_history_is_partial_not_invented(self):
        self.raw['history'] = self.raw['history'][1:]
        result = self.process(self.start())
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['summary']['counts']['assigned'], 0)
        self.assertEqual(len(result['summary']['skipped']), 1)
        self.assertEqual(result['summary']['skipped'][0]['reason_code'], 'creation_missing')
        self.assertEqual(result['summary']['skipped'][0]['stage'], 'normalize')
        self.assertEqual(self.received, [])

    def test_delayed_new_before_period_reaches_codex(self):
        self.raw['task']['createdDate'] = '2026-09-30T09:00:00+05:00'
        self.raw['history'][0]['createdDate'] = '2026-09-30T09:01:14+05:00'
        result = self.process(self.start())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['summary']['counts']['completed'], 1)
        self.assertEqual(len(self.received), 1)

    def test_list_failure_stops_before_codex_and_hides_remote_error(self):
        self.reject_list = True
        ident = self.start()
        result = self.process(ident)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stage'], 'get_tasks')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(self.received, [])

    def test_codex_failure_does_not_fallback_and_cleans_temporary_material(self):
        ident = self.start()
        with patch('worklog.bitrix_pipeline.analyze_socket', side_effect=ValueError('SECRET')) as call:
            result = self.app.process(ident)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['stage'], 'codex')
        self.assertEqual(call.call_count, 1)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertFalse((self.root / 'work' / ident).exists())

    def test_unknown_source_in_codex_answer_is_rejected(self):
        with patch('worklog.bitrix_pipeline.analyze_socket', return_value={'summary': 'ok', 'findings': [
            {'claim': 'x', 'status': 'confirmed', 'sources': ['made-up']}]}):
            result = self.app.process(self.start())
        self.assertEqual(result['status'], 'failed')

    def blocked(self):
        self.raw['task']['description'] = 'Контакт: unknown@example.test'
        ident = self.start()
        result = self.process(ident)
        self.assertEqual(result['status'], 'awaiting_privacy_decision')
        self.assertEqual(self.received, [])
        return ident, result['privacy'][0]

    def test_privacy_wait_keeps_files_and_deduplicates(self):
        ident, material = self.blocked()
        self.assertTrue((self.root / 'work' / ident).exists())
        self.assertNotIn('text', material)
        self.assertIn('unknown@example.test', material['fragment'])
        self.assertEqual(self.start(), ident)
        with self.assertRaises(ValueError):
            self.app.decide_privacy(ident, 1782, '0' * 64, 'continue')

    def test_privacy_skip_continues_without_sending_task(self):
        ident, material = self.blocked()
        self.app.decide_privacy(ident, 1782, material['version'], 'skip')
        result = self.process(ident)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.received, [])

    def test_one_skip_leaves_next_blocked_task_waiting_for_separate_decision(self):
        self.page_two = True
        ident, material = self.blocked()
        before = self.app.status(ident)
        self.assertEqual(before['pending_privacy_count'], 2)
        self.assertEqual(len(before['privacy']), 1)
        result = self.app.decide_privacy(ident, 1782, material['version'], 'skip')
        self.assertEqual(result['decision_applied']['task_id'], 1782)
        self.assertEqual(result['decision_applied']['scope'], 'single_task')
        self.assertEqual(result['status'], 'awaiting_privacy_decision')
        self.assertEqual(result['pending_privacy_count'], 1)
        self.assertEqual(result['privacy'][0]['task_id'], 1783)
        self.assertEqual(result['skipped_task_ids'], [1782])
        self.assertEqual(self.received, [])
        with self.assertRaises(ValueError):
            self.app.decide_privacy(ident, 1782, material['version'], 'skip')
        state = json.loads((self.root / 'work' / ident / 'pipeline.json').read_text(encoding='utf-8'))
        self.assertEqual([m['status'] for m in state['materials']], ['skipped', 'awaiting_privacy_decision'])
        events = [json.loads(s) for s in (self.root / 'logs' / f'{ident}.jsonl').read_text(encoding='utf-8').splitlines()]
        decisions = [e for e in events if e['stage'] == 'privacy_decision']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['task_id'], 1782)
        self.assertEqual(decisions[0]['action'], 'skip')

    def test_cannot_decide_for_another_task_before_it_is_presented(self):
        self.page_two = True
        ident, _ = self.blocked()
        state = json.loads((self.root / 'work' / ident / 'pipeline.json').read_text(encoding='utf-8'))
        second = state['materials'][1]
        with self.assertRaises(ValueError):
            self.app.decide_privacy(ident, second['task_id'], second['version'], 'skip')
        self.assertEqual(self.app.status(ident)['skipped_task_ids'], [])

    def test_skip_one_still_analyzes_other_checked_task(self):
        import hashlib
        self.page_two = True
        ident, first = self.blocked()
        path = self.root / 'work' / ident / 'pipeline.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        second = state['materials'][1]
        second['text'] = second['text'].replace('unknown@example.test', '[контакт удалён]')
        second.update(status='policy_checked', findings=[],
                      version=hashlib.sha256(second['text'].encode()).hexdigest())
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        decision = self.app.decide_privacy(ident, 1782, first['version'], 'skip')
        self.assertEqual(decision['status'], 'queued')
        result = self.process(ident)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]['source_ids'], ['bitrix:1783'])
        report = self.app.report(ident)
        self.assertEqual([a['task_id'] for a in report['analyses']], [1783])
        self.assertEqual([s['task_id'] for s in report['skipped']], [1782])
        self.assertEqual(report['counts']['assigned'], 2)

    def test_checked_task_is_analyzed_while_other_waits_and_is_not_sent_twice(self):
        self.page_two = True
        self.safe_second = True
        self.raw['task']['description'] = 'Контакт: unknown@example.test'
        ident = self.start()
        result = self.process(ident)
        self.assertEqual(result['status'], 'awaiting_privacy_decision')
        self.assertEqual(result['analyzed_task_ids'], [1783])
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]['source_ids'], ['bitrix:1783'])
        pending = result['privacy'][0]
        self.app.decide_privacy(ident, 1782, pending['version'], 'skip')
        result = self.process(ident)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(self.received), 1)
        report = self.app.report(ident)
        self.assertEqual([s['task_id'] for s in report['skipped']], [1782])
        self.assertEqual([a['task_id'] for a in report['analyses']], [1783])

    def test_restart_marks_running_interrupted_and_preserves_waiting_material(self):
        waiting, _ = self.blocked()
        other = self.app.start('EMP_001', '2026-10-01', '2026-10-30')['run_id']
        folder = self.root / 'work' / other
        folder.mkdir()
        (folder / 'private.txt').write_text('synthetic', encoding='utf-8')
        with self.app.db:
            self.app.db.execute("UPDATE jobs SET status='running' WHERE id=?", (other,))
        self.assertEqual(self.app.recover_interrupted(), 1)
        self.assertEqual(self.app.status(other)['status'], 'interrupted')
        self.assertFalse(folder.exists())
        self.assertEqual(self.app.status(waiting)['status'], 'awaiting_privacy_decision')
        self.assertTrue((self.root / 'work' / waiting).exists())

    def test_privacy_continue_applies_only_to_shown_version(self):
        ident, material = self.blocked()
        self.app.decide_privacy(ident, 1782, material['version'], 'continue')
        result = self.process(ident)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.received[0]['status'], 'user_approved_exception')
        self.assertIn('unknown@example.test', self.received[0]['text'])

    def test_privacy_clean_rechecks_before_resuming(self):
        ident, material = self.blocked()
        status = self.app.decide_privacy(ident, 1782, material['version'], 'clean', '[контакт удалён]')
        self.assertEqual(status['status'], 'queued')
        result = self.process(ident)
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('unknown@example.test', self.received[0]['text'])

    def test_privacy_bad_clean_stays_blocked_with_new_version(self):
        ident, material = self.blocked()
        status = self.app.decide_privacy(ident, 1782, material['version'], 'clean', 'other@example.test')
        self.assertEqual(status['status'], 'awaiting_privacy_decision')
        self.assertNotEqual(status['privacy'][0]['version'], material['version'])
        with self.assertRaises(ValueError):
            self.app.decide_privacy(ident, 1782, material['version'], 'continue')

    def test_later_text_and_results_are_not_sent_to_codex(self):
        self.raw['task']['changedDate'] = '2026-11-02T12:00:00+05:00'
        self.raw['task']['description'] = 'LATE-DESCRIPTION'
        self.raw['results'].append({'text': 'LATE-RESULT', 'createdAt': '2026-11-02T12:00:00+05:00'})
        result = self.process(self.start())
        self.assertEqual(result['status'], 'succeeded')
        self.assertNotIn('LATE-', self.received[0]['text'])

    def test_reassigned_task_is_included_at_historical_cutoff(self):
        self.raw['task']['responsibleId'] = '777'
        self.raw['history'].append(event(4, '2026-11-02T12:00:00+05:00', 'RESPONSIBLE_ID', '122', '777'))
        result = self.process(self.start())
        self.assertEqual(result['summary']['counts']['assigned'], 1)

    def test_unix_deadlines_in_history_and_iso_dates_in_task_are_compatible(self):
        before = '2026-10-03T18:00:00+05:00'
        after = self.raw['task']['deadline']
        self.raw['history'].append(event(4, '2026-10-03T10:00:00+05:00', 'DEADLINE',
            str(int(datetime.fromisoformat(before).timestamp())), str(int(datetime.fromisoformat(after).timestamp()))))
        result = self.process(self.start())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['summary']['counts']['overdue'], 1)

    def test_cli_receives_only_masked_material_and_returns_validated_answer(self):
        script = self.root / 'fake_codex.py'
        script.write_text('''import json,sys,pathlib
text=sys.stdin.read()
assert 'Иванов' not in text and 'SECRET' not in text
target=sys.argv[sys.argv.index('--output-last-message')+1]
pathlib.Path(target).write_text(json.dumps({'summary':'EMP_001: выполнено','findings':[]}),encoding='utf-8')
''', encoding='utf-8')
        def fake_socket(bundle, config):
            self.received.append(bundle)
            return run_cli(bundle, 'codex_cli', 'test-model', 10,
                           command=[sys.executable, str(script)], workdir=self.root)
        with patch('worklog.bitrix_pipeline.analyze_socket', side_effect=fake_socket):
            result = self.app.process(self.start())
        self.assertEqual(result['status'], 'succeeded')
        self.assertIn('Иванов Иван', result['summary']['analysis_summary'])

    @unittest.skipUnless(shutil.which('node'), 'Node.js не установлен')
    def test_mcp_to_http_to_bitrix_to_cli_and_back(self):
        from worklog.server import serve
        from concurrent.futures import ThreadPoolExecutor
        actual_server, ready = [], threading.Event()
        def create_server(address, handler):
            server = ThreadingHTTPServer(address, handler)
            actual_server.append(server)
            ready.set()
            return server
        token = 'synthetic-live-test-token-12345'
        token_path = self.root / 'local-token.txt'
        token_path.write_text(token, encoding='utf-8')
        script = self.root / 'codex_stub.py'
        script.write_text('''import json,sys,pathlib
text=sys.stdin.read()
assert 'Иванов' not in text and 'SECRET' not in text
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'summary':'EMP_001: проверено','findings':[]}),encoding='utf-8')
''', encoding='utf-8')
        def fake_socket(bundle, config):
            self.received.append(bundle)
            return run_cli(bundle, 'codex_cli', 'synthetic-model', 10,
                           command=[sys.executable, str(script)], workdir=self.root)
        with patch.dict(os.environ, {'WORKLOG_TOKEN': token}), \
                patch('worklog.server.ThreadingHTTPServer', side_effect=create_server), \
                patch('worklog.bitrix_pipeline.analyze_socket', side_effect=fake_socket):
            thread = threading.Thread(target=serve, args=(self.root, self.config, 0), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            port = actual_server[0].server_port
            process = subprocess.Popen(['node', str(Path(__file__).resolve().parents[1] / 'mcp-bridge/server.mjs'),
                '--address', f'http://127.0.0.1:{port}', '--token-file', str(token_path), '--enable-privacy'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8')
            try:
                messages = [
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                        'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                        'name': 'analyze_employee_work', 'arguments': {'employee_id': 'EMP_001',
                            'start': '2026-10-01', 'end': '2026-10-31'}}}]
                for message in messages:
                    process.stdin.write(json.dumps(message) + '\n')
                process.stdin.flush()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(lambda: [json.loads(process.stdout.readline()) for _ in range(2)])
                    try:
                        replies = future.result(timeout=20)
                    except Exception:
                        process.terminate()
                        raise
                result = json.loads(replies[1]['result']['content'][0]['text'])
                self.assertEqual(result['status'], 'succeeded')
                self.assertEqual(result['summary']['counts']['completed'], 1)
                self.assertIn('Иванов Иван', result['summary']['analysis_summary'])
                self.assertTrue(result['report_url'].startswith(f'http://127.0.0.1:{port}/view/'))
                self.assertEqual(len(self.received), 1)
            finally:
                process.terminate()
                process.communicate(timeout=5)
                actual_server[0].shutdown()
                thread.join(timeout=10)


if __name__ == '__main__':
    unittest.main()
