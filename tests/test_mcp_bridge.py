import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from employees.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'synthetic-mcp-token-123456'


@unittest.skipUnless(shutil.which('node'), 'Node.js не установлен')
class McpBridge(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        registry = Registry(self.folder / 'private' / 'registry.sqlite')
        try:
            registry.add({'full_name': 'Иванов Иван (демо)', 'aliases': [], 'bitrix_id': '99101',
                          'kaiten_id': '', 'gitlab_username': '', 'gitlab_emails': [],
                          'emails': [], 'phones': [], 'logins': []})
        finally:
            registry.close()
        tasks = json.loads((ROOT / 'examples' / 'tasks.json').read_text(encoding='utf-8'))
        tasks[0]['title'] = 'Демонстрационная проверка'
        (self.folder / 'tasks.json').write_text(json.dumps(tasks, ensure_ascii=False), encoding='utf-8')
        (self.folder / 'config.json').write_text(json.dumps({'source_file': 'tasks.json', 'demo': True}), encoding='utf-8')
        self.http = subprocess.Popen([sys.executable, '-m', 'worklog', '--data-dir', str(self.folder),
            '--config', str(self.folder / 'config.json'), 'serve', '--port', '0'], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'WORKLOG_TOKEN': TOKEN})
        self.addCleanup(self.stop_http)
        self.address = json.loads(self.http.stdout.readline())['address']
        self.start_bridge()

    def stop_http(self):
        if self.http.poll() is None:
            self.http.terminate()
        self.http.communicate(timeout=10)

    def start_bridge(self, token=TOKEN, address=None):
        token_path = self.folder / ('token-' + str(len(list(self.folder.glob('token-*')))))
        token_path.write_text(token, encoding='utf-8')
        process = subprocess.Popen(['node', str(ROOT / 'mcp-bridge' / 'server.mjs'),
            '--address', address or self.address, '--token-file', str(token_path)], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', bufsize=1)
        messages = queue.Queue()
        def read():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    messages.put({'invalid_stdout': line})
        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        self.addCleanup(self.stop_bridge, process, thread)
        self.bridge, self.messages = process, messages

    def stop_bridge(self, process, thread):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        process.stdin.close()
        thread.join(timeout=5)
        process.stdout.close()
        errors = process.stderr.read()
        process.stderr.close()
        self.assertNotIn(TOKEN, errors)

    def send(self, method, params=None, ident=None):
        message = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            message['params'] = params
        if ident is not None:
            message['id'] = ident
        self.bridge.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.bridge.stdin.flush()

    def receive(self):
        message = self.messages.get(timeout=10)
        self.assertNotIn('invalid_stdout', message)
        self.assertNotIn(TOKEN, json.dumps(message))
        return message

    def response(self, ident):
        notifications = []
        for _ in range(50):
            message = self.receive()
            if message.get('id') == ident:
                return message, notifications
            self.assertNotIn('id', message)
            notifications.append(message)
        self.fail('Response not received')

    def initialize(self, version='2025-11-25'):
        self.send('initialize', {'protocolVersion': version, 'capabilities': {},
                                'clientInfo': {'name': 'unittest', 'version': '1'}}, ident=1)
        reply, _ = self.response(1)
        self.assertEqual(reply['result']['protocolVersion'], version)
        self.assertEqual(set(reply['result']['capabilities']), {'tools'})
        self.send('notifications/initialized')

    def test_initialize_lists_exactly_two_tools_without_paths_or_secrets(self):
        self.initialize()
        self.send('tools/list', ident=2)
        reply, _ = self.response(2)
        tools = reply['result']['tools']
        self.assertEqual([tool['name'] for tool in tools], ['resolve_employee', 'get_employee_statistics'])
        for tool in tools:
            self.assertFalse(tool['inputSchema']['additionalProperties'])
        self.assertNotIn('token', json.dumps(tools))
        self.assertNotIn('source_file', json.dumps(tools))

    def test_older_protocol_version_is_negotiated(self):
        self.initialize('2024-11-05')
        self.send('ping', ident=2)
        self.assertEqual(self.response(2)[0]['result'], {})

    def test_lookup_and_statistics_through_real_http_application(self):
        self.initialize()
        self.send('tools/call', {'name': 'resolve_employee', 'arguments': {'query': 'Иванов'}}, ident=2)
        reply, _ = self.response(2)
        candidates = json.loads(reply['result']['content'][0]['text'])['candidates']
        self.assertEqual(candidates, [{'employee_id': 'EMP_001', 'name': 'Иванов Иван (демо)'}])
        self.send('tools/call', {'name': 'get_employee_statistics', 'arguments': {
            'employee_id': 'EMP_001', 'start': '2026-10-01', 'end': '2026-10-31'},
            '_meta': {'progressToken': 'test-progress'}}, ident=3)
        reply, progress = self.response(3)
        self.assertFalse(reply['result']['isError'])
        result = json.loads(reply['result']['content'][0]['text'])
        self.assertEqual(result['summary']['counts'], {'assigned': 1, 'completed': 1, 'overdue': 1, 'current': 0})
        self.assertTrue(result['summary']['demo'])
        self.assertEqual(result['summary']['completed_tasks'][0]['title'], 'Демонстрационная проверка')
        self.assertEqual(result['summary']['current_tasks'], [])
        self.assertNotIn('overdue_tasks', result['summary'])
        self.assertIn('report_url', result)
        link_text = reply['result']['content'][1]['text']
        self.assertIn('[Открыть отчёт](' + result['report_url'] + ')', link_text)
        self.assertIn('Браузер не открывался', link_text)
        self.assertNotIn('source_file', json.dumps(result))
        values = [item['params']['progress'] for item in progress]
        self.assertTrue(values)
        self.assertEqual(values, sorted(set(values)))
        self.assertTrue(all(item['params']['progressToken'] == 'test-progress' for item in progress))

    def test_calls_before_initialize_are_rejected(self):
        self.send('tools/list', ident=2)
        self.assertIn('error', self.response(2)[0])

    def test_extra_arguments_and_unknown_tools_are_rejected(self):
        self.initialize()
        for ident, params in enumerate([
                {'name': 'resolve_employee', 'arguments': {'query': 'Иванов', 'url': 'https://external.test'}},
                {'name': 'resolve_employee', 'arguments': {'query': ''}},
                {'name': 'get_employee_statistics', 'arguments': {'employee_id': 'EMP_001', 'start': 'bad', 'end': 'bad'}},
                {'name': 'read_file', 'arguments': {'path': 'registry.sqlite'}}], 2):
            with self.subTest(params=params):
                self.send('tools/call', params, ident=ident)
                self.assertEqual(self.response(ident)[0]['error']['code'], -32602)

    def test_bad_token_returns_safe_tool_error(self):
        self.start_bridge(token='wrong-token-1234567890')
        self.initialize()
        self.send('tools/call', {'name': 'resolve_employee', 'arguments': {'query': 'Иванов'}}, ident=2)
        reply, _ = self.response(2)
        self.assertTrue(reply['result']['isError'])
        self.assertNotIn('wrong-token', json.dumps(reply))

    def test_parse_error_does_not_break_next_valid_request(self):
        self.bridge.stdin.write('{broken\n')
        self.bridge.stdin.flush()
        self.assertEqual(self.receive()['error']['code'], -32700)
        self.initialize()

    def test_oversized_message_is_rejected(self):
        self.bridge.stdin.write('x' * 70000 + '\n')
        self.bridge.stdin.flush()
        self.assertEqual(self.receive()['error']['code'], -32600)
        self.assertNotEqual(self.bridge.wait(timeout=10), 0)

    def test_unknown_method_is_rejected(self):
        self.initialize()
        self.send('resources/read', {'uri': 'file:///private/registry.sqlite'}, ident=2)
        self.assertEqual(self.response(2)[0]['error']['code'], -32601)

    def test_cancellation_suppresses_result_but_keeps_connection_usable(self):
        self.initialize()
        messages = [
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': 'resolve_employee', 'arguments': {'query': 'Иванов'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 2}},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'}]
        self.bridge.stdin.write(''.join(json.dumps(message) + '\n' for message in messages))
        self.bridge.stdin.flush()
        self.assertEqual(self.response(3)[0]['result'], {})
        with self.assertRaises(queue.Empty):
            self.messages.get(timeout=0.2)

    def test_unknown_protocol_offers_supported_version(self):
        self.send('initialize', {'protocolVersion': '2099-01-01', 'capabilities': {},
                                'clientInfo': {'name': 'unittest', 'version': '1'}}, ident=1)
        self.assertEqual(self.response(1)[0]['result']['protocolVersion'], '2025-11-25')

    def test_fragmented_message_is_reassembled(self):
        self.bridge.stdin.write('{"jsonrpc":"2.0","id":2,')
        self.bridge.stdin.flush()
        self.bridge.stdin.write('"method":"ping"}\n')
        self.bridge.stdin.flush()
        self.assertEqual(self.response(2)[0]['result'], {})

    def test_missing_token_file_fails_without_printing_configuration(self):
        result = subprocess.run(['node', str(ROOT / 'mcp-bridge' / 'server.mjs'),
            '--token-file', str(self.folder / 'missing-token')], cwd=ROOT,
            capture_output=True, encoding='utf-8', timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertNotIn(str(self.folder), result.stderr)


if __name__ == '__main__':
    unittest.main()
