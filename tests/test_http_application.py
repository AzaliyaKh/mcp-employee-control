import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'local-test-secret-123456'


class LocalHttpApplication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'WORKLOG_TOKEN': TOKEN}
        added = subprocess.run([
            sys.executable, '-m', 'employees', '--db', str(self.root / 'private' / 'registry.sqlite'), 'add'],
            cwd=ROOT, input='\n'.join(['Иванов Иван', '', '123', '', '', '', '', '', '']) + '\n',
            capture_output=True, encoding='utf-8', env=env, timeout=10)
        self.assertEqual(added.returncode, 0, added.stderr)
        tasks = self.root / 'tasks.json'
        tasks.write_text('[]', encoding='utf-8')
        config = self.root / 'config.json'
        config.write_text(json.dumps({'source_file': str(tasks)}), encoding='utf-8')
        self.server = subprocess.Popen([
            sys.executable, '-m', 'worklog', '--data-dir', str(self.root), '--config', str(config),
            'serve', '--port', '0'], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', env=env)
        self.addCleanup(self.stop)
        line = self.server.stdout.readline()
        if not line:
            error = self.server.stderr.read()
            self.fail('HTTP-приложение не запустилось: ' + error)
        self.address = json.loads(line)['address']

    def stop(self):
        if self.server.poll() is None:
            self.server.terminate()
        self.server.communicate(timeout=10)

    def request(self, path, data=None, token=TOKEN):
        request = Request(self.address + path,
                          data=json.dumps(data).encode('utf-8') if data is not None else None,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_http_starts_job_and_returns_its_permanent_report(self):
        code, _ = self.request('/health', token='wrong')
        self.assertEqual(code, 401)
        code, started = self.request('/analysis', {
            'kind': 'employee_statistics', 'employee_id': 'EMP_001',
            'start': '2026-10-01', 'end': '2026-10-31'})
        self.assertEqual(code, 202)
        status = started
        deadline = time.monotonic() + 10
        while status['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(0.05)
            code, status = self.request('/analysis/' + started['run_id'])
            self.assertEqual(code, 200)
        self.assertEqual(status['status'], 'succeeded')
        code, report = self.request('/reports/' + status['report_id'])
        self.assertEqual(code, 200)
        self.assertEqual(report['employee_name'], 'Иванов Иван')

    def test_report_link_opens_only_its_own_document_without_general_token(self):
        _, started = self.request('/analysis', {
            'kind': 'employee_statistics', 'employee_id': 'EMP_001',
            'start': '2026-10-01', 'end': '2026-10-31'})
        status = started
        deadline = time.monotonic() + 10
        while status['status'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(0.05)
            _, status = self.request('/analysis/' + started['run_id'])
        self.assertIn('report_url', status)
        with urlopen(status['report_url'], timeout=5) as response:
            self.assertIn('text/html', response.headers['Content-Type'])
            self.assertIn('Иванов Иван', response.read().decode('utf-8'))
        invalid = status['report_url'].replace(started['run_id'], 'a' * 32)
        with self.assertRaises(HTTPError) as error:
            urlopen(invalid, timeout=5)
        self.assertEqual(error.exception.code, 401)
        self.assertNotIn(TOKEN, status['report_url'])

    def test_employee_lookup_returns_only_minimal_candidates(self):
        code, data = self.request('/employees?' + urlencode({'query': 'Иванов'}))
        self.assertEqual(code, 200)
        self.assertEqual(data, {'candidates': [{'employee_id': 'EMP_001', 'name': 'Иванов Иван'}]})

    @unittest.skipUnless(shutil.which('node'), 'Node.js не установлен')
    def test_mcp_http_client_waits_for_result_and_returns_summary_not_full_report(self):
        client_url = (ROOT / 'mcp-bridge' / 'client.mjs').as_uri()
        script = f'''import {{ WorklogClient }} from {json.dumps(client_url)};
const client = new WorklogClient({{ baseUrl: process.env.WORKLOG_ADDRESS,
                                  token: process.env.WORKLOG_TOKEN }});
const result = await client.statistics({{ employeeId: 'EMP_001', start: '2026-10-01',
                                         end: '2026-10-31' }}, {{ pollMs: 20 }});
console.log(JSON.stringify(result));'''
        result = subprocess.run(['node', '--input-type=module', '-e', script],
            cwd=ROOT, capture_output=True, encoding='utf-8', timeout=15,
            env={**os.environ, 'WORKLOG_ADDRESS': self.address, 'WORKLOG_TOKEN': TOKEN})
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['summary']['counts']['assigned'], 0)
        self.assertIn('report_url', data)
        self.assertNotIn('source_file', result.stdout)
        self.assertNotIn(TOKEN, result.stdout)

    def test_http_rejects_model_supplied_paths_and_analyzer_settings(self):
        code, _ = self.request('/analysis', {
            'kind': 'employee_statistics', 'employee_id': 'EMP_001',
            'start': '2026-10-01', 'end': '2026-10-31',
            'source_file': 'C:/secret.txt', 'model': 'other-model'})
        self.assertEqual(code, 400)
        code, _ = self.request('/reports/../../private/registry.sqlite')
        self.assertEqual(code, 404)

    def test_invalid_date_does_not_crash_http_request_handler(self):
        code, _ = self.request('/analysis', {
            'kind': 'employee_statistics', 'employee_id': 'EMP_001',
            'start': '9999-01-01', 'end': '9999-12-31'})
        self.assertEqual(code, 400)


if __name__ == '__main__':
    unittest.main()
