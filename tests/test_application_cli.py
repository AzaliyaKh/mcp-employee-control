import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ApplicationCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db = self.home / 'private' / 'registry.sqlite'
        self.tasks = self.home / 'tasks.json'
        self.tasks.write_text(json.dumps([{
            'key': 'bitrix:main:123', 'title': 'Задача', 'history_complete': True,
            'created_at': '2026-09-20T10:00:00+05:00', 'events': [
                {'at': '2026-09-20T10:00:00+05:00', 'type': 'assignment', 'employee': 'EMP_001'},
                {'at': '2026-09-20T10:00:00+05:00', 'type': 'status', 'value': 'in_progress'},
            ],
        }]), encoding='utf-8')
        self.config = self.home / 'config.json'
        self.config.write_text(json.dumps({'source_file': str(self.tasks)}), encoding='utf-8')
        added = self.command('employees', '--db', self.db, 'add', answers='\n'.join([
            'Иванов Иван', 'Иванов', '123', '', 'ivanov', '', '', '', '']) + '\n')
        self.assertEqual(added.returncode, 0, added.stderr)

    def command(self, module, *args, answers=''):
        return subprocess.run([sys.executable, '-m', module, *map(str, args)], cwd=ROOT,
                              input=answers, capture_output=True, encoding='utf-8', timeout=15,
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def app(self, *args):
        return self.command('worklog', '--data-dir', self.home, '--config', self.config, *args)

    def result(self, process):
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def test_statistics_job_saves_permanent_report_and_cleans_work_files(self):
        started = self.result(self.app('start', '--employee', 'EMP_001',
                                       '--start', '2026-10-01', '--end', '2026-10-31'))
        job = self.result(self.app('process', started['run_id']))
        self.assertEqual(job['status'], 'succeeded')
        report = self.result(self.app('report', job['report_id']))
        self.assertEqual(report['employee_name'], 'Иванов Иван')
        self.assertEqual(report['counts']['current'], 1)
        self.assertFalse((self.home / 'work' / started['run_id']).exists())
        status = self.result(self.app('status', started['run_id']))
        self.assertEqual(status['report_id'], job['report_id'])
        self.assertTrue((self.home / 'reports' / job['report_id'] / 'report.json').is_file())

    def test_active_duplicate_reuses_run_but_finished_request_can_be_repeated(self):
        first = self.result(self.app('start', '--employee', 'EMP_001',
                                     '--start', '2026-10-01', '--end', '2026-10-31'))
        duplicate = self.result(self.app('start', '--employee', 'EMP_001',
                                         '--start', '2026-10-01', '--end', '2026-10-31'))
        self.assertEqual(duplicate['run_id'], first['run_id'])
        self.result(self.app('process', first['run_id']))
        repeated = self.result(self.app('start', '--employee', 'EMP_001',
                                        '--start', '2026-10-01', '--end', '2026-10-31'))
        self.assertNotEqual(repeated['run_id'], first['run_id'])

    def test_missing_source_records_stage_and_cause_without_creating_report(self):
        started = self.result(self.app('start', '--employee', 'EMP_001',
                                       '--start', '2026-10-01', '--end', '2026-10-31'))
        self.tasks.unlink()
        job = self.result(self.app('process', started['run_id']))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['stage'], 'collect')
        self.assertIn('не найден', job['error'])
        self.assertIsNone(job['report_id'])
        self.assertFalse((self.home / 'work' / started['run_id']).exists())
        log = (self.home / 'logs' / f"{started['run_id']}.jsonl").read_text(encoding='utf-8')
        self.assertIn('этап остановлен', log)

    def test_demo_input_is_identified_in_report_and_model_summary(self):
        self.config.write_text(json.dumps({'source_file': str(self.tasks), 'demo': True}), encoding='utf-8')
        started = self.result(self.app('start', '--employee', 'EMP_001',
                                       '--start', '2026-10-01', '--end', '2026-10-31'))
        done = self.result(self.app('process', started['run_id']))
        self.assertTrue(done['summary']['demo'])
        report = self.result(self.app('report', done['report_id']))
        self.assertEqual(report['source_mode'], 'normalized_file')
        self.assertTrue(report['demo'])


if __name__ == '__main__':
    unittest.main()
