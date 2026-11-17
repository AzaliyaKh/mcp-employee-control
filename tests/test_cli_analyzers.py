import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analyzer.client import analyze
from analyzer.cli_runner import docker_executable, run_container
from analyzer.cli_worker import run_cli


def bundle():
    return {'protocol': 1, 'status': 'policy_checked', 'text': 'EMP_001 completed SOURCE_001',
            'version': hashlib.sha256(b'EMP_001 completed SOURCE_001').hexdigest(),
            'employee_labels': ['EMP_001'], 'source_ids': ['SOURCE_001']}


class CliAnalyzers(unittest.TestCase):
    def test_docker_discovery_uses_path_when_available(self):
        with patch('analyzer.cli_runner.shutil.which', return_value='fake-docker'):
            self.assertEqual(docker_executable(), 'fake-docker')

    def test_docker_discovery_supports_per_user_desktop_install(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'Programs/DockerDesktop/resources/bin/docker.exe'
            executable.parent.mkdir(parents=True)
            executable.touch()
            with patch('analyzer.cli_runner.shutil.which', return_value=None), \
                    patch.dict(os.environ, {'LOCALAPPDATA': directory}):
                self.assertEqual(docker_executable(), str(executable))

    def test_worker_refuses_host_execution_before_starting_cli(self):
        from analyzer.cli_worker import main
        with patch('analyzer.cli_worker.sys.platform', 'win32'), \
                patch('analyzer.cli_worker.subprocess.run') as execute, \
                patch('analyzer.cli_worker.sys.stderr'):
            self.assertEqual(main(), 1)
            execute.assert_not_called()

    def config(self, kind='codex_cli'):
        return {'kind': kind, 'model': 'test-model', 'image': 'worklog-analyzer:test',
                'gateway_volume': 'worklog-codex-gateway', 'timeout_seconds': 30}

    def test_both_adapters_use_same_handoff_and_validate_findings(self):
        for kind in ('codex_cli', 'claude_code'):
            with self.subTest(kind=kind), patch('analyzer.cli_runner.run_container',
                    return_value={'summary': 'EMP_999', 'findings': []}) as runner:
                with self.assertRaises(ValueError):
                    analyze(bundle(), self.config(kind))
                self.assertEqual(runner.call_args.args[0], bundle())

    def test_bad_handoff_never_starts_container(self):
        data = bundle()
        data['text'] = 'modified'
        with patch('analyzer.cli_runner.run_container') as runner:
            with self.assertRaises(ValueError):
                analyze(data, self.config())
            runner.assert_not_called()

    def test_container_has_no_network_or_host_mount_and_only_provider_secret(self):
        seen = []
        def execute(args, **kwargs):
            seen.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, '{"summary":"EMP_001","findings":[]}', '')
        with patch.dict(os.environ, {'CODEX_API_KEY': 'fake-provider', 'GITLAB_TOKEN': 'private-source'}), \
                patch('analyzer.cli_runner.subprocess.run', side_effect=execute):
            run_container(bundle(), self.config())
        args, options = seen[0]
        self.assertIn('--network=none', args)
        self.assertIn('--read-only', args)
        self.assertIn('--cap-drop=ALL', args)
        self.assertIn('--security-opt=no-new-privileges', args)
        self.assertIn('type=volume,source=worklog-codex-gateway,target=/gateway,readonly', args)
        self.assertFalse(any('bind' in value for value in args))
        self.assertNotIn('fake-provider', ' '.join(args))
        self.assertNotIn('GITLAB_TOKEN', options['env'])
        self.assertNotIn('private-source', options['input'])
        self.assertFalse(options.get('shell', False))
        self.assertEqual(len(seen), 2)  # run then unconditional cleanup

    def test_failure_and_timeout_are_safe_and_cleanup_container(self):
        for failure in (subprocess.TimeoutExpired('docker', 1),
                        subprocess.CompletedProcess([], 1, '', 'sensitive provider output')):
            with self.subTest(failure=failure), patch.dict(os.environ, {'CODEX_API_KEY': 'fake'}):
                def execute(args, **kwargs):
                    if args[1] == 'rm':
                        return subprocess.CompletedProcess(args, 0)
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with patch('analyzer.cli_runner.subprocess.run', side_effect=execute) as run:
                    with self.assertRaises(ValueError) as error:
                        run_container(bundle(), self.config())
                    self.assertNotIn('sensitive', str(error.exception))
                    self.assertEqual(run.call_args.args[0][1:3], ['rm', '-f'])

    def test_invalid_config_or_missing_secret_does_not_start_docker(self):
        for extra in ({'gateway_volume': '/private'}, {'timeout_seconds': 0}, {'image': '--privileged'}):
            with self.subTest(extra=extra), patch('analyzer.cli_runner.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    run_container(bundle(), {**self.config(), **extra})
                run.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), patch('analyzer.cli_runner.subprocess.run') as run:
            with self.assertRaises(ValueError):
                run_container(bundle(), self.config())
            run.assert_not_called()

    def fake_cli(self, directory, kind, failure=False):
        script = Path(directory) / 'fake.py'
        script.write_text('''import json, pathlib, sys
prompt = sys.stdin.read()
assert 'EMP_001' in prompt
assert 'source_ids' in prompt
args = sys.argv[1:]
result = {'summary': 'EMP_001', 'findings': []}
if '--output-last-message' in args:
    pathlib.Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(result))
else:
    assert '--tools' in args and args[args.index('--tools') + 1] == ''
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                      'structured_output': result}))
''', encoding='utf-8')
        return [sys.executable, str(script)]

    def test_codex_exec_reads_stdin_and_final_file(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(bundle(), 'codex_cli', 'test-model', 10,
                             command=self.fake_cli(directory, 'codex_cli'), workdir=directory)
            self.assertEqual(result, {'summary': 'EMP_001', 'findings': []})

    def test_claude_print_reads_stdin_and_structured_output(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(bundle(), 'claude_code', 'test-model', 10,
                             command=self.fake_cli(directory, 'claude_code'), workdir=directory)
            self.assertEqual(result, {'summary': 'EMP_001', 'findings': []})

    def test_worker_rejects_error_envelope_and_unfinished_codex(self):
        for kind, output in [('claude_code', '{"type":"result","is_error":true}'),
                             ('codex_cli', '')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, \
                    patch('analyzer.cli_worker.subprocess.run',
                          return_value=subprocess.CompletedProcess([], 0, output, 'secret')):
                with self.assertRaises(ValueError):
                    run_cli(bundle(), kind, 'test-model', 10, workdir=directory)
