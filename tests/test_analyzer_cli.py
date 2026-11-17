from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AnalyzerCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.received = []
        self.api_key = 'test-key'
        self.content = json.dumps({'summary': 'EMP_001 выполнил работу.', 'findings': []})
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                fixture.received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                response = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                    'role': 'assistant', 'content': fixture.content}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.config = self.folder / 'analyzer.json'
        self.config.write_text(json.dumps({'kind': 'chat_completions', 'model': 'test-model',
            'base_url': f'http://127.0.0.1:{self.server.server_port}/v1/'}), encoding='utf-8')
        self.db = self.folder / 'registry.sqlite'
        self.run_dir = self.folder / 'run'
        self.handoff = self.folder / 'handoff.json'
        self.reply = self.folder / 'reply.json'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def command(self, module, *args, answers=''):
        return subprocess.run([sys.executable, '-m', module, *map(str, args)], cwd=ROOT,
            input=answers, capture_output=True, encoding='utf-8', timeout=15,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'ANALYZER_API_KEY': self.api_key})

    def prepare(self):
        added = self.command('employees', '--db', self.db, 'add', answers='\n'.join([
            'Иванов Иван', '', '123', '', 'ivanov', '', '', '', '']) + '\n')
        self.assertEqual(added.returncode, 0, added.stderr)
        source = self.folder / 'input.txt'
        source.write_text('Иванов Иван выполнил задачу.', encoding='utf-8')
        masked = self.command('privacy', '--db', self.db, 'prepare', source, '--run-dir', self.run_dir)
        self.assertEqual(masked.returncode, 0, masked.stderr)
        exported = self.command('privacy', 'export', self.run_dir, '--bundle', '--output', self.handoff)
        self.assertEqual(exported.returncode, 0, exported.stderr)

    def analyze(self):
        return self.command('analyzer', '--config', self.config, '--input', self.handoff, '--output', self.reply)

    def test_analyzer_receives_only_prepared_text_and_returns_validated_findings(self):
        self.prepare()
        result = self.analyze()
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(self.reply.read_text(encoding='utf-8'))
        self.assertEqual(response['summary'], 'EMP_001 выполнил работу.')
        self.assertEqual(len(self.received), 1)
        request = self.received[0]
        self.assertEqual(request['model'], 'test-model')
        self.assertNotIn('tools', request)
        self.assertNotIn('Иванов', json.dumps(request, ensure_ascii=False))
        self.assertNotIn('test-key', result.stdout + result.stderr)

    def test_confirmed_claim_must_point_to_the_transferred_text_source(self):
        self.prepare()
        self.content = json.dumps({'summary': 'Проверено.', 'findings': [{
            'claim': 'Работа указана в отчёте', 'status': 'confirmed', 'sources': ['SOURCE_001']}]})
        result = self.analyze()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.reply.read_text(encoding='utf-8'))['findings'][0]['sources'],
                         ['SOURCE_001'])

    def test_unknown_evidence_is_rejected_without_saving_success(self):
        self.prepare()
        self.content = json.dumps({'summary': 'Проверено.', 'findings': [{
            'claim': 'Выдуманное доказательство', 'status': 'confirmed', 'sources': ['SOURCE_999']}]})
        result = self.analyze()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('неизвестный источник', result.stderr)
        self.assertFalse(self.reply.exists())

    def test_modified_handoff_is_rejected_before_network_request(self):
        self.prepare()
        bundle = json.loads(self.handoff.read_text(encoding='utf-8'))
        bundle['text'] = 'Новые непроверенные данные'
        self.handoff.write_text(json.dumps(bundle), encoding='utf-8')
        result = self.analyze()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.received, [])
        self.assertFalse(self.reply.exists())

    def test_unknown_employee_label_is_rejected(self):
        self.prepare()
        self.content = json.dumps({'summary': 'EMP_999 выполнил работу.', 'findings': []})
        result = self.analyze()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.reply.exists())

    def test_private_mapping_cannot_be_added_to_handoff(self):
        self.prepare()
        bundle = json.loads(self.handoff.read_text(encoding='utf-8'))
        bundle['names'] = {'EMP_001': 'Иванов Иван'}
        self.handoff.write_text(json.dumps(bundle), encoding='utf-8')
        result = self.analyze()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.received, [])

    def test_invalid_key_header_does_not_leak_secret_in_error(self):
        self.prepare()
        self.api_key = 'sensitive-test-secret\nbroken'
        result = self.analyze()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('sensitive-test-secret', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.received, [])


if __name__ == '__main__':
    unittest.main()
