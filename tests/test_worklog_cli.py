import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorklogCommands(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'tasks.json'

    def stats(self, tasks, employee='EMP_001'):
        self.source.write_text(json.dumps(tasks, ensure_ascii=False), encoding='utf-8')
        return subprocess.run([
            sys.executable, '-m', 'worklog', 'statistics', str(self.source),
            '--employee', employee, '--start', '2026-10-01', '--end', '2026-10-31',
        ], cwd=ROOT, capture_output=True, encoding='utf-8', timeout=15,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})

    def task(self, ident, assigned='2026-09-20T10:00:00+05:00',
             deadline=None, closed=None, employee='EMP_001'):
        events = [
            {'at': assigned, 'type': 'assignment', 'employee': employee},
            {'at': assigned, 'type': 'status', 'value': 'in_progress'},
        ]
        if deadline:
            events.append({'at': assigned, 'type': 'deadline', 'value': deadline})
        if closed:
            events.append({'at': closed, 'type': 'status', 'value': 'completed'})
        return {'key': f'bitrix:main:{ident}', 'title': f'Задача {ident}',
                'created_at': assigned, 'history_complete': True, 'events': events}

    def test_statistics_uses_october_snapshot_and_counts_only_october_deadline_breaches(self):
        result = self.stats([
            self.task(1, deadline='2026-10-10T18:00:00+05:00',
                      closed='2026-10-09T18:00:00+05:00'),
            self.task(2, deadline='2026-10-15T18:00:00+05:00'),
            self.task(3, deadline='2026-09-25T18:00:00+05:00'),
            self.task(4, deadline='2026-10-20T18:00:00+05:00',
                      closed='2026-11-02T18:00:00+05:00'),
            self.task(5, employee='EMP_002'),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['counts'], {'assigned': 4, 'completed': 1,
                                            'overdue': 2, 'current': 3})
        self.assertEqual(report['overdue'], ['bitrix:main:2', 'bitrix:main:4'])
        self.assertEqual(report['timezone'], 'Asia/Yekaterinburg')

    def test_instant_reassignment_is_not_a_day_of_employee_work(self):
        task = self.task(1, assigned='2026-10-05T10:00:00+05:00')
        task['events'].append({'at': '2026-10-05T10:00:00+05:00',
                               'type': 'assignment', 'employee': 'EMP_002'})
        result = self.stats([task])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['counts']['assigned'], 0)

    def test_deadline_history_prevents_late_extension_from_hiding_a_breach(self):
        after = self.task(1, deadline='2026-10-10T18:00:00+05:00')
        after['events'].append({'at': '2026-10-12T10:00:00+05:00', 'type': 'deadline',
                                'value': '2026-11-10T18:00:00+05:00'})
        before = self.task(2, deadline='2026-10-10T18:00:00+05:00')
        before['events'].append({'at': '2026-10-09T10:00:00+05:00', 'type': 'deadline',
                                 'value': '2026-11-10T18:00:00+05:00'})
        result = self.stats([after, before])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['overdue'], ['bitrix:main:1'])

    def test_missing_history_is_explicitly_partial_and_not_fabricated(self):
        task = self.task(1)
        task['history_complete'] = False
        result = self.stats([task])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report['partial'])
        self.assertEqual(report['unavailable'], ['bitrix:main:1'])
        self.assertEqual(report['counts']['assigned'], 0)

    def test_reopened_task_is_current_and_not_completed_at_end_of_period(self):
        task = self.task(1, closed='2026-10-10T10:00:00+05:00')
        task['events'].append({'at': '2026-10-15T10:00:00+05:00',
                               'type': 'status', 'value': 'in_progress'})
        result = self.stats([task])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['completed'], [])
        self.assertEqual(report['current'], ['bitrix:main:1'])

    def test_timezone_offsets_are_compared_as_instants(self):
        # 30 September 20:00 UTC is already 1 October in Yekaterinburg.
        task = self.task(1, closed='2026-09-30T20:00:00+00:00')
        result = self.stats([task])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['completed'], ['bitrix:main:1'])


if __name__ == '__main__':
    unittest.main()
