import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class EmployeesCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'registry.sqlite'

    def command(self, *args, answers=''):
        return subprocess.run(
            [sys.executable, '-m', 'employees', '--db', str(self.db), *args],
            input=answers, encoding='utf-8', capture_output=True, cwd=ROOT,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'}, timeout=15,
        )

    def add(self, name='Иванов Иван', bitrix='123', gitlab='ivanov',
            kaiten='456', commit_email='ivanov@example.test'):
        answers = '\n'.join([name, '', bitrix, kaiten, gitlab,
                              commit_email, '', '', '']) + '\n'
        return self.command('add', answers=answers)

    def records(self):
        result = self.command('list', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def csv_file(self, rows, name='employees.csv'):
        path = Path(self.temp.name) / name
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                'full_name', 'aliases', 'bitrix_id', 'kaiten_id', 'gitlab_username',
                'gitlab_emails', 'emails', 'phones', 'logins'])
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_add_and_list_preserve_employee_between_command_processes(self):
        result = self.add()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('EMP_001', result.stdout)
        employee = self.records()[0]
        self.assertEqual(employee['label'], 'EMP_001')
        self.assertEqual(employee['full_name'], 'Иванов Иван')
        self.assertEqual(employee['bitrix_id'], '123')
        self.assertEqual(employee['gitlab_emails'], ['ivanov@example.test'])

    def test_edit_preserves_label_and_old_name_and_accounts(self):
        self.assertEqual(self.add().returncode, 0)
        answers = '\n'.join(['Иванов Пётр', '', '', '', 'petrov',
                              'petrov@example.test', '', '', '']) + '\n'
        result = self.command('edit', 'EMP_001', answers=answers)
        self.assertEqual(result.returncode, 0, result.stderr)
        employee = self.records()[0]
        self.assertEqual(employee['label'], 'EMP_001')
        self.assertEqual(employee['full_name'], 'Иванов Пётр')
        self.assertEqual(employee['bitrix_id'], '123')
        self.assertEqual(employee['gitlab_username'], 'petrov')
        self.assertIn('Иванов Иван', employee['history']['full_name'])
        self.assertIn('ivanov', employee['history']['gitlab_username'])
        self.assertIn('ivanov@example.test', employee['history']['gitlab_emails'])

    def test_invalid_employee_does_not_create_record_or_consume_label(self):
        missing_name = self.add(name='  ')
        self.assertNotEqual(missing_name.returncode, 0)
        self.assertIn('ФИО', missing_name.stderr)
        missing_account = self.add(bitrix='', kaiten='', gitlab='', commit_email='')
        self.assertNotEqual(missing_account.returncode, 0)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.add().returncode, 0)
        self.assertEqual(self.records()[0]['label'], 'EMP_001')

    def test_import_skips_existing_employee_without_overwriting(self):
        self.assertEqual(self.add().returncode, 0)
        before = self.records()[0]
        path = self.csv_file([
            {'full_name': 'Иванов Иван', 'bitrix_id': '123', 'phones': '999'},
            {'full_name': 'Петров Пётр', 'bitrix_id': '234'},
        ])
        imported = self.command('import', str(path), answers='да\n')
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertIn('Существующих: 1', imported.stdout)
        self.assertIn('EMP_001', imported.stdout)
        self.assertEqual(self.records()[0], before)
        self.assertEqual(self.records()[1]['label'], 'EMP_002')

    def test_invalid_csv_reports_row_and_valid_candidates_without_partial_write(self):
        path = self.csv_file([
            {'full_name': 'Петров Пётр', 'bitrix_id': '234'},
            {'full_name': '', 'bitrix_id': '345'},
        ])
        imported = self.command('import', str(path), answers='да\n')
        self.assertNotEqual(imported.returncode, 0)
        self.assertIn('Строка 3', imported.stderr)
        self.assertIn('Петров Пётр', imported.stdout)
        self.assertEqual(self.records(), [])

    def test_account_conflicts_including_history_do_not_change_registry(self):
        self.assertEqual(self.add().returncode, 0)
        changed = self.command('edit', 'EMP_001', answers='\n'.join([
            '', '', '', '', 'new_login', '', '', '', '']) + '\n')
        self.assertEqual(changed.returncode, 0, changed.stderr)
        before = self.records()
        conflict = self.add(name='Другой Человек', bitrix='999', kaiten='888',
                            gitlab='IVANOV', commit_email='other@example.test')
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn('EMP_001', conflict.stderr)
        self.assertEqual(self.records(), before)
        # Identical names are allowed when external identities are distinct.
        success = self.add(bitrix='999', kaiten='888', gitlab='second',
                           commit_email='second@example.test')
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual([item['label'] for item in self.records()],
                         ['EMP_001', 'EMP_002'])

    def test_import_saves_only_after_preview_and_confirmation(self):
        path = self.csv_file([{'full_name': 'Петров Пётр', 'bitrix_id': '234'}])
        result = self.command('import', str(path), answers='нет\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Петров Пётр', result.stdout)
        self.assertIn('Новых: 1', result.stdout)
        self.assertIn('отменён', result.stdout)
        self.assertEqual(self.records(), [])
        confirmed = self.command('import', str(path), answers='да\n')
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        self.assertEqual(self.records()[0]['full_name'], 'Петров Пётр')
        self.assertEqual(self.records()[0]['label'], 'EMP_001')

    def test_import_rejects_duplicate_csv_columns_instead_of_losing_values(self):
        path = Path(self.temp.name) / 'bad.csv'
        path.write_text('full_name,bitrix_id,bitrix_id\nИванов Иван,123,999\n',
                        encoding='utf-8')
        result = self.command('import', str(path), answers='да\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('столбц', result.stderr)
        self.assertEqual(self.records(), [])

    def test_id_spelling_cannot_assign_same_bitrix_account_to_two_people(self):
        self.assertEqual(self.add().returncode, 0)
        result = self.add(name='Петров Пётр', bitrix='00123', kaiten='',
                          gitlab='', commit_email='')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.records()), 1)

    def test_edit_does_not_overwrite_a_newer_change_from_another_command(self):
        self.assertEqual(self.add().returncode, 0)
        with subprocess.Popen(
            [sys.executable, '-m', 'employees', '--db', str(self.db), 'edit', 'EMP_001'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', cwd=ROOT,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        ) as pending:
            try:
                # First editor has displayed its stored snapshot and is waiting for input.
                pending.stdout.readline()
                prompt = ''
                while not prompt.endswith(': '):
                    char = pending.stdout.read(1)
                    self.assertTrue(char, 'Редактор завершился до запроса ввода')
                    prompt += char
                newer = self.command('edit', 'EMP_001', answers='\n'.join([
                    'Иванов Пётр', '', '', '', '', '', '', '', '']) + '\n')
                self.assertEqual(newer.returncode, 0, newer.stderr)
                _, error = pending.communicate('\n' * 9, timeout=10)
                self.assertNotEqual(pending.returncode, 0)
                self.assertIn('изменены', error)
            finally:
                if pending.poll() is None:
                    pending.kill()
                    pending.communicate()
        self.assertEqual(self.records()[0]['full_name'], 'Иванов Пётр')

    def test_unclosed_csv_quote_is_reported_without_traceback(self):
        path = Path(self.temp.name) / 'broken.csv'
        path.write_text('full_name,bitrix_id\n"Иванов Иван,123\n', encoding='utf-8')
        result = self.command('import', str(path), answers='да\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('CSV', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.records(), [])

    def test_incomplete_add_and_import_confirmation_are_cancelled(self):
        result = self.command('add', answers='Иванов Иван\n')
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self.records(), [])
        path = self.csv_file([{'full_name': 'Петров Пётр', 'bitrix_id': '234'}])
        result = self.command('import', str(path))
        self.assertEqual(result.returncode, 130)
        self.assertEqual(self.records(), [])

    def test_conflicting_edit_rolls_back_and_clearing_optional_field_is_explicit(self):
        self.assertEqual(self.add().returncode, 0)
        self.assertEqual(self.add(name='Петров Пётр', bitrix='234', kaiten='567',
                                 gitlab='petrov', commit_email='petrov@example.test').returncode, 0)
        before = self.records()
        result = self.command('edit', 'EMP_002', answers='\n'.join([
            '', '', '123', '', '', '', '', '', '']) + '\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.records(), before)
        result = self.command('edit', 'EMP_002', answers='\n'.join([
            '', '', '-', '', '', '', '', '', '']) + '\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.records()[1]['bitrix_id'], '')
        self.assertIn('234', self.records()[1]['history']['bitrix_id'])

    def test_conflicting_csv_accounts_are_reported_before_confirmation(self):
        path = self.csv_file([
            {'full_name': 'Первый Сотрудник', 'bitrix_id': '234'},
            {'full_name': 'Второй Сотрудник', 'bitrix_id': '234'},
        ])
        result = self.command('import', str(path), answers='да\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Строка 3', result.stderr)
        self.assertNotIn('Сохранить', result.stdout)
        self.assertEqual(self.records(), [])

    def test_import_rechecks_conflicts_and_rolls_back_entire_batch(self):
        path = self.csv_file([
            {'full_name': 'Первый Сотрудник', 'bitrix_id': '234'},
            {'full_name': 'Второй Сотрудник', 'bitrix_id': '345'},
        ])
        with subprocess.Popen(
            [sys.executable, '-m', 'employees', '--db', str(self.db), 'import', str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding='utf-8', cwd=ROOT,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        ) as pending:
            try:
                output = ''
                while not output.endswith('[да/нет]: '):
                    char = pending.stdout.read(1)
                    self.assertTrue(char, 'Импорт завершился до подтверждения')
                    output += char
                concurrent = self.add(name='Другой Человек', bitrix='345')
                self.assertEqual(concurrent.returncode, 0, concurrent.stderr)
                _, error = pending.communicate('да\n', timeout=10)
                self.assertNotEqual(pending.returncode, 0)
                self.assertIn('EMP_001', error)
            finally:
                if pending.poll() is None:
                    pending.kill()
                    pending.communicate()
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['full_name'], 'Другой Человек')


if __name__ == '__main__':
    unittest.main()
