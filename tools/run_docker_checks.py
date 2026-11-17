"""One-command offline acceptance checks; uses a fresh synthetic database per test."""
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import uuid


PROJECT = Path(__file__).resolve().parents[1] if '--inside' not in sys.argv else Path('/app')
RUN_FOLDER = None


class OfflineChecks(unittest.TestCase):
    def setUp(self):
        self.folder = RUN_FOLDER / self._testMethodName
        self.folder.mkdir()
        self.db = self.folder / 'employees.sqlite'

    def command(self, module, *args, answers='', ok=True):
        result = subprocess.run([sys.executable, '-m', module, *map(str, args)],
            input=answers, capture_output=True, encoding='utf-8', timeout=20,
            cwd=PROJECT, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
        return result

    def employee(self, *args, **kwargs):
        return self.command('employees', '--db', self.db, *args, **kwargs)

    def add(self, name='Test Employee', bitrix='99101', login='test_author', **kwargs):
        return self.employee('add', answers='\n'.join([
            name, '', bitrix, '', login, 'author@example.test', '', '', '']) + '\n', **kwargs)

    def records(self):
        return json.loads(self.employee('list', '--json').stdout)

    def write(self, name, text):
        path = self.folder / name
        path.write_text(text, encoding='utf-8')
        return path

    def csv(self, name, rows):
        path = self.folder / name
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['full_name', 'bitrix_id'])
            writer.writeheader()
            writer.writerows(rows)
        return path

    def prepare(self, text='Test Employee finished the task.', name='run'):
        if not self.db.exists():
            self.add()
        source = self.write(name + '.txt', text)
        folder = self.folder / name
        result = self.command('privacy', '--db', self.db, 'prepare', source, '--run-dir', folder)
        return folder, json.loads(result.stdout)

    def export(self, folder, name='export.json', ok=True):
        output = self.folder / name
        result = self.command('privacy', 'export', folder, '--bundle', '--output', output, ok=ok)
        if not ok:
            self.assertFalse(output.exists())
        return output, result

    def test_01_empty_registry(self):
        self.assertEqual(self.records(), [])

    def test_02_add_and_read_in_another_process(self):
        self.add()
        record = self.records()[0]
        self.assertEqual(record['label'], 'EMP_001')
        self.assertEqual(record['full_name'], 'Test Employee')
        self.assertEqual(record['gitlab_emails'], ['author@example.test'])

    def test_03_reject_duplicate_account_without_changing_database(self):
        self.add()
        before = self.records()
        self.add(name='Other Employee', login='other', ok=False)
        self.assertEqual(self.records(), before)

    def test_04_reject_empty_name_without_consuming_label(self):
        self.add(name='', ok=False)
        self.assertEqual(self.records(), [])
        self.add()
        self.assertEqual(self.records()[0]['label'], 'EMP_001')

    def test_05_edit_preserves_label_and_history(self):
        self.add()
        self.employee('edit', 'EMP_001', answers='\n'.join([
            'Updated Employee', '', '', '', 'updated_author', '', '', '', '']) + '\n')
        record = self.records()[0]
        self.assertEqual(record['label'], 'EMP_001')
        self.assertEqual(record['full_name'], 'Updated Employee')
        self.assertIn('Test Employee', record['history']['full_name'])

    def test_06_csv_import(self):
        source = self.csv('valid.csv', [{'full_name': 'CSV First', 'bitrix_id': '99201'},
                                        {'full_name': 'CSV Second', 'bitrix_id': '99202'}])
        self.employee('import', source, answers='yes\n')
        self.assertEqual([row['label'] for row in self.records()], ['EMP_001', 'EMP_002'])

    def test_07_csv_cancel_does_not_save(self):
        source = self.csv('cancel.csv', [{'full_name': 'CSV First', 'bitrix_id': '99201'}])
        self.employee('import', source, answers='no\n')
        self.assertEqual(self.records(), [])

    def test_08_invalid_csv_is_atomic(self):
        source = self.csv('invalid.csv', [{'full_name': 'CSV First', 'bitrix_id': '99201'},
                                          {'full_name': '', 'bitrix_id': '99202'}])
        self.employee('import', source, answers='yes\n', ok=False)
        self.assertEqual(self.records(), [])

    def test_09_repeated_csv_does_not_overwrite(self):
        self.add()
        before = self.records()
        source = self.csv('repeat.csv', [{'full_name': 'Test Employee', 'bitrix_id': '99101'}])
        self.employee('import', source, answers='yes\n')
        self.assertEqual(self.records(), before)

    def test_10_mask_known_name_login_and_email(self):
        folder, state = self.prepare('Test Employee / test_author / author@example.test')
        self.assertEqual(state['status'], 'policy_checked')
        self.assertEqual(state['findings'], [])
        self.assertEqual((folder / 'candidate.txt').read_text(encoding='utf-8'),
                         'EMP_001 / EMP_001 / EMP_001')

    def test_11_export_has_no_private_mapping(self):
        folder, _ = self.prepare()
        output, _ = self.export(folder)
        payload = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(set(payload), {'protocol', 'status', 'version', 'text',
                                        'employee_labels', 'source_ids'})
        self.assertEqual(payload['employee_labels'], ['EMP_001'])
        self.assertNotIn('Test Employee', output.read_text(encoding='utf-8'))

    def test_12_restore_name(self):
        folder, _ = self.prepare()
        answer = self.write('answer.txt', 'EMP_001 finished.')
        output = self.folder / 'restored.txt'
        self.command('privacy', 'restore', folder, answer, '--output', output)
        self.assertEqual(output.read_text(encoding='utf-8'), 'Test Employee finished.')

    def test_13_unknown_answer_label_does_not_create_output(self):
        folder, _ = self.prepare()
        answer = self.write('answer.txt', 'EMP_999 finished.')
        output = self.folder / 'restored.txt'
        self.command('privacy', 'restore', folder, answer, '--output', output, ok=False)
        self.assertFalse(output.exists())

    def test_14_unknown_email_blocks_export(self):
        folder, state = self.prepare('Contact: unknown@example.test')
        self.assertEqual(state['status'], 'awaiting_privacy_decision')
        self.assertTrue(state['findings'])
        self.export(folder, ok=False)

    def test_15_wrong_version_does_not_authorize(self):
        folder, _ = self.prepare('Contact: unknown@example.test')
        self.command('privacy', 'decide', folder, 'continue', '--version', 'wrong', ok=False)
        self.export(folder, ok=False)

    def test_16_continue_authorizes_only_shown_synthetic_material(self):
        folder, state = self.prepare('Contact: unknown@example.test')
        self.command('privacy', 'decide', folder, 'continue', '--version', state['version'])
        output, _ = self.export(folder)
        payload = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(payload['status'], 'user_approved_exception')
        self.assertIn('unknown@example.test', payload['text'])

    def test_17_skip_keeps_export_blocked(self):
        folder, state = self.prepare('Contact: unknown@example.test')
        self.command('privacy', 'decide', folder, 'skip', '--version', state['version'])
        self.export(folder, ok=False)

    def test_18_changed_material_invalidates_check(self):
        folder, _ = self.prepare()
        (folder / 'candidate.txt').write_text('Changed material', encoding='utf-8')
        self.export(folder, ok=False)

    def test_19_restore_does_not_overwrite_existing_file(self):
        folder, _ = self.prepare()
        answer = self.write('answer.txt', 'EMP_001 finished.')
        output = self.write('existing.txt', 'Keep original')
        self.command('privacy', 'restore', folder, answer, '--output', output, ok=False)
        self.assertEqual(output.read_text(encoding='utf-8'), 'Keep original')

    def statistics(self, tasks):
        source = self.write('tasks.json', json.dumps(tasks))
        return json.loads(self.command('worklog', 'statistics', source, '--employee', 'EMP_001',
                                      '--start', '2026-10-01', '--end', '2026-10-31').stdout)

    def task(self):
        return {'key': 'synthetic:1', 'created_at': '2026-09-20T10:00:00+05:00',
                'history_complete': True, 'events': [
                    {'at': '2026-09-20T10:00:00+05:00', 'type': 'assignment', 'employee': 'EMP_001'},
                    {'at': '2026-09-20T10:00:00+05:00', 'type': 'deadline', 'value': '2026-10-10T18:00:00+05:00'},
                    {'at': '2026-10-12T14:00:00+05:00', 'type': 'status', 'value': 'completed'}]}

    def test_20_statistics_completed_and_overdue(self):
        report = self.statistics([self.task()])
        self.assertEqual(report['counts'], {'assigned': 1, 'completed': 1, 'overdue': 1, 'current': 0})
        self.assertFalse(report['partial'])

    def test_21_empty_statistics(self):
        self.assertEqual(self.statistics([])['counts'],
                         {'assigned': 0, 'completed': 0, 'overdue': 0, 'current': 0})

    def test_22_incomplete_history_is_not_counted(self):
        task = self.task()
        task['history_complete'] = False
        report = self.statistics([task])
        self.assertTrue(report['partial'])
        self.assertEqual(report['unavailable'], ['synthetic:1'])
        self.assertEqual(report['counts']['completed'], 0)

    def test_23_completion_after_period_is_excluded(self):
        task = self.task()
        task['events'][-1]['at'] = '2026-11-02T14:00:00+05:00'
        report = self.statistics([task])
        self.assertEqual(report['counts']['completed'], 0)
        self.assertEqual(report['counts']['current'], 1)

    def test_24_reassignment_does_not_transfer_completion(self):
        task = self.task()
        task['events'].append({'at': '2026-10-20T10:00:00+05:00',
                               'type': 'assignment', 'employee': 'EMP_002'})
        self.assertEqual(self.statistics([task])['counts']['completed'], 1)


def run_checks(base):
    global RUN_FOLDER
    RUN_FOLDER = base / ('docker-checks-' + uuid.uuid4().hex[:12])
    RUN_FOLDER.mkdir(parents=True, exist_ok=False)
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(OfflineChecks))
    text = output.getvalue()
    print(text)
    report = {'tests': result.testsRun, 'failures': len(result.failures),
              'errors': len(result.errors), 'skipped': len(result.skipped),
              'success': result.wasSuccessful(), 'time_utc': datetime.now(timezone.utc).isoformat(),
              'python': sys.version.split()[0], 'platform': sys.platform,
              'synthetic_data_only': True, 'external_services_called': False}
    (RUN_FOLDER / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    (RUN_FOLDER / 'report.txt').write_text(text, encoding='utf-8')
    print('DOCKER_CHECKS_SUMMARY=' + json.dumps(report))
    print('REPORT_FOLDER=' + str(RUN_FOLDER))
    return 0 if result.wasSuccessful() else 1


def main():
    if '--inside' in sys.argv:
        return run_checks(Path('/data'))
    if '--local' in sys.argv:
        return run_checks(PROJECT / 'data')
    docker = shutil.which('docker')
    if not docker:
        candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/DockerDesktop/resources/bin/docker.exe'
        if candidate.is_file():
            docker = str(candidate)
    if not docker:
        print('Docker CLI not found. Open a new terminal after installing Docker.', file=sys.stderr)
        return 1
    print('Running 24 offline checks in one temporary container. No cloud requests.', flush=True)
    command = [docker, 'compose', 'run', '--rm', '-T', 'service', 'python', '-', '--inside']
    try:
        result = subprocess.run(command, cwd=PROJECT,
            input=Path(__file__).read_text(encoding='utf-8'), encoding='utf-8', timeout=300)
        return result.returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        print('Could not finish Docker checks: ' + type(error).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
