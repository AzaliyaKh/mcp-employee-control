import json
from pathlib import Path
import unittest

from integrations.bitrix_diagnostics import diagnose, sanitized
from worklog.bitrix_pipeline import HistoryError, normalize
from worklog.statistics import statistics
from test_bitrix_live import fixture, event


class BitrixDiagnostics(unittest.TestCase):
    def test_missing_creation_has_specific_safe_reason(self):
        raw = fixture()
        raw['history'] = raw['history'][1:]
        report = diagnose(raw)
        self.assertEqual(report['reason_code'], 'creation_missing')
        self.assertNotIn('Иванов', json.dumps(report, ensure_ascii=False))

    def test_snapshot_mismatch_identifies_field(self):
        raw = fixture()
        raw['task']['status'] = '3'
        with self.assertRaises(HistoryError) as caught:
            normalize(raw, {})
        self.assertEqual(caught.exception.code, 'snapshot_mismatch')
        self.assertEqual(caught.exception.field, 'STATUS')

    def test_history_gap_is_not_silently_accepted(self):
        raw = fixture()
        raw['history'][-1]['value']['from'] = '2'
        with self.assertRaises(HistoryError) as caught:
            normalize(raw, {})
        self.assertEqual(caught.exception.code, 'history_gap')

    def test_bad_date_has_no_remote_value_in_error(self):
        raw = fixture()
        raw['task']['createdDate'] = 'PERSONAL-SECRET-RAW'
        report = diagnose(raw)
        self.assertEqual(report['reason_code'], 'date_invalid')
        self.assertNotIn('PERSONAL-SECRET-RAW', json.dumps(report))

    def test_accounts_are_consistently_replaced_and_documents_are_removed(self):
        raw = fixture()
        raw['task']['responsibleId'] = '12012'
        raw['task']['description'] = 'PERSONAL-SECRET-RAW'
        raw['history'].append(event(4, '2026-10-02T10:00:00+05:00', 'RESPONSIBLE_ID', '12012', '12412'))
        raw['history'][0]['user'] = {'name': 'PERSONAL-NAME', 'login': 'secret@example.test'}
        result = sanitized(raw)
        self.assertEqual(result['task']['responsibleId'], result['history'][-1]['value']['from'])
        text = json.dumps(result)
        for value in ('12012', '12412', 'PERSONAL', 'secret@example.test', 'description', 'title'):
            self.assertNotIn(value, text)
        self.assertEqual(result['results'], [])

    def test_supported_fixture_survives_sanitization(self):
        report = diagnose(fixture())
        self.assertEqual(report['normalization'], 'succeeded')
        self.assertTrue(normalize(report['fixture'], {})['history_complete'])

    def saved_fixture(self, ident):
        path = Path(__file__).parent / 'fixtures' / 'bitrix-history' / f'task-{ident}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_delayed_new_in_saved_tasks_does_not_reject_october_history(self):
        for ident in (54, 100, 1782):
            with self.subTest(task=ident):
                raw = self.saved_fixture(ident)
                task = normalize(raw, {'900001': 'EMP_001'})
                report = statistics([task], 'EMP_001', '2026-10-23', '2026-10-31')
                self.assertFalse(report['partial'])
                self.assertEqual(report['counts'], {'assigned': 1, 'completed': 0, 'overdue': 0, 'current': 1})
                self.assertEqual(task['created_at'], raw['task']['createdDate'])

    def test_unobserved_creation_interval_is_not_invented(self):
        for ident, start, end in ((54, '2026-05-14', '2026-05-14'), (100, '2026-08-12', '2026-08-12')):
            with self.subTest(task=ident):
                task = normalize(self.saved_fixture(ident), {'900001': 'EMP_001'})
                report = statistics([task], 'EMP_001', start, end)
                self.assertTrue(report['partial'])
                self.assertEqual(report['unavailable'], [f'bitrix:{ident}'])
                self.assertEqual(report['counts']['assigned'], 0)

    def test_september_uses_historical_assignee_before_october_reassignment(self):
        task = normalize(self.saved_fixture(100), {'900001': 'EMP_001', '900005': 'EMP_002'})
        old = statistics([task], 'EMP_002', '2026-09-01', '2026-09-30')
        new = statistics([task], 'EMP_001', '2026-09-01', '2026-09-30')
        self.assertFalse(old['partial'])
        self.assertEqual(old['counts']['assigned'], 1)
        self.assertEqual(old['counts']['current'], 1)
        self.assertEqual(new['counts']['assigned'], 0)

    def test_initial_state_begins_at_new_not_at_unconfirmed_task_date(self):
        task = normalize(self.saved_fixture(54), {})
        earliest = min(e['at'] for e in task['events'])
        self.assertEqual(earliest, task['history_valid_from'])
        self.assertNotEqual(earliest, task['created_at'])

    def test_new_before_task_date_is_still_rejected(self):
        raw = fixture()
        raw['history'][0]['createdDate'] = '2026-10-01T08:59:59+05:00'
        with self.assertRaises(HistoryError) as caught:
            normalize(raw, {})
        self.assertEqual(caught.exception.code, 'creation_inconsistent')

    def test_multiple_new_records_are_not_silently_accepted(self):
        raw = fixture()
        raw['history'].append(event(5, '2026-10-02T12:00:00+05:00', 'NEW'))
        with self.assertRaises(HistoryError) as caught:
            normalize(raw, {})
        self.assertEqual(caught.exception.code, 'creation_ambiguous')

    def test_changes_before_new_are_not_silently_accepted(self):
        raw = fixture()
        raw['history'][0]['createdDate'] = '2026-10-03T12:00:00+05:00'
        with self.assertRaises(HistoryError) as caught:
            normalize(raw, {})
        self.assertEqual(caught.exception.code, 'creation_inconsistent')
