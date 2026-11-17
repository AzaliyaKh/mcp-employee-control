import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PrivacyCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.db = self.folder / 'registry.sqlite'
        self.run_dir = self.folder / 'run'
        self.source = self.folder / 'task.txt'

    def command(self, module, *args, answers=''):
        return subprocess.run(
            [sys.executable, '-m', module, *map(str, args)], cwd=ROOT,
            input=answers, capture_output=True, encoding='utf-8', timeout=15,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        )

    def employee(self, name='Иванов Иван', aliases='Иванов', login='ivanov',
                 email='ivanov@example.test', phone='+7 (912) 123-45-67', bitrix='123'):
        result = self.command('employees', '--db', self.db, 'add', answers='\n'.join([
            name, aliases, bitrix, '', login, email, '', phone, '']) + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)

    def prepare(self, text):
        self.source.write_text(text, encoding='utf-8')
        return self.command('privacy', '--db', self.db, 'prepare', self.source,
                            '--run-dir', self.run_dir)

    def result(self, process):
        self.assertEqual(process.returncode, 0, process.stderr)
        return json.loads(process.stdout)

    def export(self, filename='outbound.txt'):
        target = self.folder / filename
        process = self.command('privacy', 'export', self.run_dir, '--output', target)
        return process, target

    def test_prepare_export_and_restore_use_labels_without_exposing_mapping(self):
        self.employee()
        status = self.result(self.prepare(
            'Иванов Иван завершил задачу. ivanov@example.test; ivanov.'))
        self.assertEqual(status['status'], 'policy_checked')
        sent, target = self.export()
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(target.read_text(encoding='utf-8'),
                         'EMP_001 завершил задачу. EMP_001; EMP_001.')
        self.assertNotIn('Иванов', sent.stdout)
        answer = self.folder / 'answer.txt'
        answer.write_text('EMP_001 выполнил работу.', encoding='utf-8')
        report = self.folder / 'report.txt'
        restored = self.command('privacy', 'restore', self.run_dir, answer,
                                '--output', report)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(report.read_text(encoding='utf-8'),
                         'Иванов Иван выполнил работу.')

    def test_history_phone_format_and_word_boundaries(self):
        self.employee()
        edited = self.command('employees', '--db', self.db, 'edit', 'EMP_001',
            answers='\n'.join(['Петров Пётр', 'Петров', '', '', 'petrov',
                               'petrov@example.test', '', '', '']) + '\n')
        self.assertEqual(edited.returncode, 0, edited.stderr)
        self.result(self.prepare('ИВАНОВ ИВАН; ivanov@example.test; '
                                 '8 912 123 45 67; +79121234567; superivanov; Ивановский.'))
        result, target = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(encoding='utf-8'),
                         'EMP_001; EMP_001; EMP_001; EMP_001; superivanov; Ивановский.')

    def test_shared_name_blocks_export_and_inspect_shows_local_reason(self):
        self.employee()
        self.employee(name='Иванов Пётр', aliases='Иванов', login='petrov',
                      email='petrov@example.test', phone='', bitrix='234')
        status = self.result(self.prepare('Иванов сделал задачу.'))
        self.assertEqual(status['status'], 'awaiting_privacy_decision')
        self.assertEqual(status['findings'][0]['value'], 'Иванов')
        self.assertEqual(status['findings'][0]['labels'], ['EMP_001', 'EMP_002'])
        inspected = self.result(self.command('privacy', 'inspect', self.run_dir))
        self.assertIn('нескольким', inspected['findings'][0]['reason'])
        exported, target = self.export()
        self.assertNotEqual(exported.returncode, 0)
        self.assertFalse(target.exists())

    def test_unknown_contacts_are_reported_instead_of_assigned_to_employee(self):
        self.employee()
        status = self.result(self.prepare(
            'Иванов Иван. Контакт: outsider@example.test, +7 999 111 22 33.'))
        self.assertEqual(status['status'], 'awaiting_privacy_decision')
        self.assertEqual({item['kind'] for item in status['findings']},
                         {'unknown_email', 'unknown_phone'})
        self.assertEqual({item['value'] for item in status['findings']},
                         {'outsider@example.test', '+7 999 111 22 33'})
        process, target = self.export()
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(target.exists())

    def test_continue_requires_explicit_matching_version_and_keeps_unknown_data(self):
        self.employee()
        status = self.result(self.prepare('Иванов Иван: outsider@example.test'))
        refused = self.command('privacy', 'decide', self.run_dir, 'continue',
                                '--version', 'outdated')
        self.assertNotEqual(refused.returncode, 0)
        still_blocked, _ = self.export()
        self.assertNotEqual(still_blocked.returncode, 0)
        approved = self.result(self.command('privacy', 'decide', self.run_dir,
                                            'continue', '--version', status['version']))
        self.assertEqual(approved['status'], 'user_approved_exception')
        sent, target = self.export()
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(target.read_text(encoding='utf-8'),
                         'EMP_001: outsider@example.test')

    def test_skip_keeps_material_unavailable_for_export(self):
        self.employee()
        status = self.result(self.prepare('outsider@example.test'))
        skipped = self.result(self.command('privacy', 'decide', self.run_dir,
                                           'skip', '--version', status['version']))
        self.assertEqual(skipped['status'], 'skipped')
        process, target = self.export()
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(target.exists())

    def test_changed_candidate_cannot_reuse_approval(self):
        self.employee()
        status = self.result(self.prepare('outsider@example.test'))
        self.result(self.command('privacy', 'decide', self.run_dir, 'continue',
                                 '--version', status['version']))
        (self.run_dir / 'candidate.txt').write_text('other@example.test', encoding='utf-8')
        sent, target = self.export()
        self.assertNotEqual(sent.returncode, 0)
        self.assertIn('изменён', sent.stderr)
        self.assertFalse(target.exists())

    def test_unknown_answer_label_does_not_create_a_guessed_report(self):
        self.employee()
        self.result(self.prepare('Иванов Иван работает.'))
        answer = self.folder / 'answer.txt'
        answer.write_text('EMP_999 всё выполнил.', encoding='utf-8')
        report = self.folder / 'report.txt'
        result = self.command('privacy', 'restore', self.run_dir, answer,
                              '--output', report)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('EMP_999', result.stderr)
        self.assertFalse(report.exists())

    def test_input_labels_are_not_silently_treated_as_generated_employee_names(self):
        self.employee()
        status = self.result(self.prepare('Иванов Иван упомянул EMP_001.'))
        self.assertEqual(status['status'], 'awaiting_privacy_decision')
        self.assertIn('reserved_label', {finding['kind'] for finding in status['findings']})

    def test_restore_cannot_bypass_a_pending_decision(self):
        self.employee()
        self.result(self.prepare('Иванов: outsider@example.test'))
        answer = self.folder / 'answer.txt'
        answer.write_text('EMP_001 работает.', encoding='utf-8')
        report = self.folder / 'report.txt'
        result = self.command('privacy', 'restore', self.run_dir, answer,
                              '--output', report)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(report.exists())

    def test_missing_database_is_not_created_and_existing_runs_are_not_overwritten(self):
        result = self.prepare('Нейтральный текст.')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.db.exists())
        self.employee()
        first = self.result(self.prepare('Иванов Иван работает.'))
        second = self.prepare('Другой текст.')
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(self.result(self.command('privacy', 'inspect', self.run_dir))['version'],
                         first['version'])

    def test_corrupt_state_is_reported_without_traceback(self):
        self.employee()
        self.result(self.prepare('Иванов Иван работает.'))
        (self.run_dir / 'state.json').write_text('{}', encoding='utf-8')
        result, target = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse(target.exists())

    def test_restore_json_preserves_escaping_in_employee_name(self):
        self.employee(name='Иванов "Иван"', aliases='Иванов')
        self.result(self.prepare('Иванов работает.'))
        answer = self.folder / 'answer.json'
        answer.write_text(json.dumps({'summary': 'EMP_001 выполнил работу.', 'findings': []}),
                          encoding='utf-8')
        output = self.folder / 'report.json'
        result = self.command('privacy', 'restore', self.run_dir, answer, '--json', '--output', output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(output.read_text(encoding='utf-8'))['summary'],
                         'Иванов "Иван" выполнил работу.')


if __name__ == '__main__':
    unittest.main()
