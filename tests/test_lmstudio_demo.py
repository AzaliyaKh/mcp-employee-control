import json
from pathlib import Path
import tempfile
import unittest

from tools.start_lmstudio_demo import ROOT, prepare
from worklog.application import Application


class LmStudioDemo(unittest.TestCase):
    def setUp(self):
        (ROOT / 'data').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'data')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'demo'

    def test_demo_lookup_and_analysis_use_synthetic_registry(self):
        token = prepare(self.folder)
        self.assertGreaterEqual(len(token), 16)
        app = Application(self.folder, self.folder / 'config.json')
        self.addCleanup(app.close)
        candidates = app.resolve_employee('Иванов')['candidates']
        self.assertEqual(candidates, [{'employee_id': 'EMP_001', 'name': 'Иванов Иван (демо)'}])
        run = app.start('EMP_001', '2026-10-01', '2026-10-31')
        app.process(run['run_id'])
        result = app.status(run['run_id'])
        self.assertEqual(result['summary']['counts'],
                         {'assigned': 1, 'completed': 1, 'overdue': 1, 'current': 0})
        self.assertTrue(result['summary']['demo'])

    def test_restart_preserves_token_and_data(self):
        token = prepare(self.folder)
        database = self.folder / 'private' / 'registry.sqlite'
        before = database.read_bytes()
        self.assertEqual(prepare(self.folder), token)
        self.assertEqual(database.read_bytes(), before)

    def test_existing_unmarked_folder_is_not_modified(self):
        self.folder.mkdir()
        existing = self.folder / 'private.txt'
        existing.write_text('keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            prepare(self.folder)
        self.assertEqual(existing.read_text(encoding='utf-8'), 'keep')
        self.assertEqual(list(self.folder.iterdir()), [existing])

    def test_paths_outside_project_data_are_rejected(self):
        for folder in (ROOT, ROOT / 'data', ROOT.parent / 'other'):
            with self.subTest(folder=folder), self.assertRaises(ValueError):
                prepare(folder)
