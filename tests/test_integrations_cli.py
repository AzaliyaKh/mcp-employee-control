from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


class DirectSourceCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.requests = []
        self.responses = {
            '/bitrix/tasks.task.get': {'result': {'task': {'id': '123', 'description': 'GitLab: https://gitlab.test/group/project'}}},
            '/bitrix/tasks.task.result.list': {'result': [{'id': 1, 'text': 'Работа выполнена', 'files': [77]}]},
            '/bitrix/tasks.task.history.list': {'result': {'list': []}},
        }
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                fixture.requests.append((self.path, json.loads(body or b'{}'), dict(self.headers)))
                self.respond()

            def do_GET(self):
                fixture.requests.append((self.path, None, dict(self.headers)))
                self.respond()

            def respond(self):
                data = fixture.responses.get(urlsplit(self.path).path)
                payload = json.dumps(data).encode('utf-8')
                self.send_response(200 if data is not None else 404)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.config = self.folder / 'integrations.json'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def collect(self, config, *args):
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.output = self.folder / 'collected.json'
        return subprocess.run([
            sys.executable, '-m', 'integrations', '--config', str(self.config),
            *args, '--output', str(self.output)], cwd=ROOT, capture_output=True,
            encoding='utf-8', timeout=15, env={**os.environ,
                'PYTHONIOENCODING': 'utf-8', 'TEST_KAITEN_TOKEN': 'kaiten-test-token',
                'TEST_GITLAB_TOKEN': 'gitlab-test-token'})

    def test_bitrix_gets_task_results_without_fetching_chat(self):
        result = self.collect({'bitrix': {'base_url': self.base + '/bitrix/'}},
                              'bitrix-task', '123')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(data['results'][0]['files'], [77])
        self.assertEqual(data['task']['id'], '123')
        self.assertFalse(any('chat' in path or 'commentitem' in path for path, _, _ in self.requests))
        self.assertEqual(self.requests[0][1]['taskId'], 123)

    def test_kaiten_reads_configured_report_property_without_guessing_chat_messages(self):
        self.responses['/kaiten/cards/234'] = {
            'id': 234, 'title': 'Задача', 'description': 'Описание',
            'properties': {'id_10': 'Реализована функция; изменён app.py'},
        }
        result = self.collect({'kaiten': {'base_url': self.base + '/kaiten/',
            'token_env': 'TEST_KAITEN_TOKEN', 'report_property_id': 10}}, 'kaiten-card', '234')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(data['report_status'], 'available')
        self.assertIn('app.py', data['report'])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0][2]['Authorization'], 'Bearer kaiten-test-token')

    def test_gitlab_filters_by_registry_email_and_marks_missing_diffs(self):
        db = self.folder / 'registry.sqlite'
        added = subprocess.run([sys.executable, '-m', 'employees', '--db', str(db), 'add'],
            cwd=ROOT, capture_output=True, encoding='utf-8', timeout=10,
            input='\n'.join(['Иванов Иван', '', '', '', 'ivanov', 'ivanov@example.test', '', '', '']) + '\n',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        self.assertEqual(added.returncode, 0, added.stderr)
        sha = 'a' * 40
        self.responses['/gitlab/projects/group%2Fproject/repository/commits'] = [
            {'id': sha, 'author_email': 'IVANOV@example.test', 'committed_date': '2026-10-05T10:00:00Z'},
            {'id': 'b' * 40, 'author_email': 'other@example.test', 'committed_date': '2026-10-05T10:00:00Z'},
        ]
        self.responses[f'/gitlab/projects/group%2Fproject/repository/commits/{sha}/diff'] = [
            {'new_path': 'app.py', 'old_path': 'app.py', 'diff': '', 'too_large': True},
        ]
        result = self.collect({'gitlab': {'base_url': self.base + '/gitlab/',
            'token_env': 'TEST_GITLAB_TOKEN'}}, 'gitlab-changes', '--db', str(db),
            '--employee', 'EMP_001', '--project', 'group/project',
            '--since', '2026-10-01T00:00:00+05:00', '--until', '2026-11-01T00:00:00+05:00')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual([commit['id'] for commit in data['commits']], [sha])
        self.assertTrue(data['partial'])
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('gitlab-test-token', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
