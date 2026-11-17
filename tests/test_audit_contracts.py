"""Synthetic API contracts; all HTTP traffic stays on loopback."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from analyzer.client import analyze
from integrations.sources import Bitrix, Kaiten, GitLab
from integrations.transport import JsonTransport
from worklog.statistics import statistics


class ApiContracts(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reply = lambda path, query, body: (200, {}, {})
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                url = urlsplit(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                fixture.calls.append((url.path, body))
                status, headers, data = fixture.reply(url.path, parse_qs(url.query), body)
                payload = json.dumps(data).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f'http://127.0.0.1:{self.server.server_port}/'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_bitrix_results_preserve_multiple_reports_and_pagination(self):
        def reply(path, query, body):
            if path.endswith('get'):
                return 200, {}, {'result': {'task': {'id': '123'}}}
            if path.endswith('history.list'):
                return 200, {}, {'result': {'list': []}}
            if body['start'] == 0:
                return 200, {}, {'result': [{'id': 1, 'text': 'Report', 'files': [77],
                    'createdBy': 5, 'createdAt': '2026-10-05T10:00:00+05:00'}], 'next': 50}
            return 200, {}, {'result': [{'id': 2, 'text': '', 'files': [78]}]}
        self.reply = reply
        result = Bitrix(self.base).task(123)
        self.assertEqual([item['id'] for item in result['results']], [1, 2])
        self.assertEqual(result['results'][0]['createdBy'], 5)
        self.assertEqual(result['results'][1]['files'], [78])
        self.assertFalse(result['history_complete'])
        self.assertEqual(self.calls[2][1]['start'], 50)

    def test_bitrix_application_error_is_rejected(self):
        self.reply = lambda *args: (200, {}, {'error': 'DENIED', 'error_description': 'synthetic secret'})
        with self.assertRaises(ValueError) as error:
            Bitrix(self.base).task(123)
        self.assertNotIn('synthetic secret', str(error.exception))

    def test_bitrix_repeated_page_is_rejected(self):
        self.reply = lambda *args: (200, {}, {'result': [], 'next': 0})
        with self.assertRaises(ValueError):
            Bitrix(self.base)._list('tasks.task.result.list', 123)

    def test_kaiten_null_missing_and_structured_properties_are_distinct(self):
        for properties, expected in [(None, 'absent'), ({}, 'absent'),
                                      ({'id_10': {'files': [77]}}, 'unsupported')]:
            with self.subTest(properties=properties):
                self.reply = lambda *args: (200, {}, {'id': 234, 'properties': properties})
                self.assertEqual(Kaiten(self.base, 'fake', 10).card(234)['report_status'], expected)
        self.assertEqual(Kaiten(self.base, 'fake').card(234)['report_status'], 'not_configured')

    def test_kaiten_malformed_properties_fail_with_validation_error(self):
        for properties in ([], ['invalid'], 'invalid'):
            with self.subTest(properties=properties):
                self.reply = lambda *args: (200, {}, {'id': 234, 'properties': properties})
                with self.assertRaises(ValueError):
                    Kaiten(self.base, 'fake', 10).card(234)

    def test_gitlab_short_page_still_follows_next_page_header(self):
        self.reply = lambda path, query, body: (200,
            {'X-Next-Page': '2' if query['page'] == ['1'] else ''},
            [{'id': query['page'][0]}])
        self.assertEqual(GitLab(self.base, 'fake')._pages('commits'), [{'id': '1'}, {'id': '2'}])
        self.assertEqual(len(self.calls), 2)

    def changes(self):
        return GitLab(self.base, 'fake').changes('group/project',
            {'label': 'EMP_001', 'gitlab_emails': ['new@example.test'],
             'history': {'gitlab_emails': ['old@example.test']}},
            '2026-10-01T00:00:00+05:00', '2026-11-01T00:00:00+05:00')

    def test_gitlab_historical_author_and_exclusive_end(self):
        sha = 'a' * 40
        self.reply = lambda path, *args: (200, {},
            [{'new_path': 'app.py', 'collapsed': True}] if path.endswith('/diff') else [
                {'id': sha, 'author_email': 'OLD@example.test', 'committed_date': '2026-10-01T00:00:00+05:00'},
                {'id': 'b' * 40, 'author_email': 'new@example.test', 'committed_date': '2026-11-01T00:00:00+05:00'}])
        result = self.changes()
        self.assertEqual([item['id'] for item in result['commits']], [sha])
        self.assertTrue(result['partial'])
        self.assertEqual(len(self.calls), 2)

    def test_gitlab_malformed_commit_is_rejected(self):
        self.reply = lambda *args: (200, {}, ['invalid'])
        with self.assertRaises(ValueError):
            self.changes()

    def test_gitlab_malformed_diff_is_rejected(self):
        self.reply = lambda path, *args: (200, {}, [None] if path.endswith('/diff') else [
            {'id': 'a' * 40, 'author_email': 'new@example.test', 'committed_date': '2026-10-05T00:00:00Z'}])
        with self.assertRaises(ValueError):
            self.changes()

    def test_http_failures_do_not_expose_response_body(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.reply = lambda *args: (status, {}, {'secret': 'fake-sensitive-value'})
                with self.assertRaises(ValueError) as error:
                    JsonTransport(self.base).request('test')
                self.assertIn(str(status), str(error.exception))
                self.assertNotIn('fake-sensitive-value', str(error.exception))

    def test_redirect_is_not_followed_with_credentials(self):
        self.reply = lambda *args: (302, {'Location': self.base + 'other'}, {})
        with self.assertRaises(ValueError):
            JsonTransport(self.base, {'Authorization': 'Bearer fake'}).request('test')
        self.assertEqual(len(self.calls), 1)

    def test_malformed_analyzer_envelopes_are_validation_errors(self):
        bundle = {'protocol': 1, 'status': 'policy_checked', 'text': 'EMP_001',
            'version': hashlib.sha256(b'EMP_001').hexdigest(),
            'employee_labels': ['EMP_001'], 'source_ids': ['SOURCE_001']}
        config = {'kind': 'chat_completions', 'model': 'fake', 'base_url': self.base}
        for response in ({'choices': []}, {'choices': [None]},
                         {'choices': [{'finish_reason': 'stop', 'message': None}]},
                         {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}):
            with self.subTest(response=response), patch.dict(os.environ, {'ANALYZER_API_KEY': 'fake'}):
                self.reply = lambda *args: (200, {}, response)
                with self.assertRaises(ValueError):
                    analyze(bundle, config)


class StatisticsOwnership(unittest.TestCase):
    def test_reassignment_after_completion_does_not_transfer_completed_work(self):
        task = {'key': 'bitrix:main:1', 'created_at': '2026-10-01T00:00:00+05:00',
                'history_complete': True, 'events': [
                    {'at': '2026-10-01T00:00:00+05:00', 'type': 'assignment', 'employee': 'EMP_001'},
                    {'at': '2026-10-10T00:00:00+05:00', 'type': 'status', 'value': 'completed'},
                    {'at': '2026-10-20T00:00:00+05:00', 'type': 'assignment', 'employee': 'EMP_002'}]}
        self.assertEqual(statistics([task], 'EMP_001', '2026-10-01', '2026-10-31')['counts']['completed'], 1)
        self.assertEqual(statistics([task], 'EMP_002', '2026-10-01', '2026-10-31')['counts']['completed'], 0)
