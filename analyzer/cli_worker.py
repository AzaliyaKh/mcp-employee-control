"""Container-only worker. CLI output is not forwarded to application logs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .client import INSTRUCTIONS, validate_findings, validate_handoff


SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['summary', 'findings'],
    'properties': {'summary': {'type': 'string'}, 'findings': {
        'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['claim', 'status', 'sources'], 'properties': {
                'claim': {'type': 'string'}, 'status': {'type': 'string',
                    'enum': ['confirmed', 'partially_confirmed', 'not_found']},
                'sources': {'type': 'array', 'items': {'type': 'string'}}}}}}}


def run_cli(bundle, kind, model, timeout, *, command=None, workdir=None):
    validate_handoff(bundle)
    if kind not in ('codex_cli', 'claude_code'):
        raise ValueError('Неизвестный CLI-анализатор')
    prompt = INSTRUCTIONS + '\n' + json.dumps({key: bundle[key] for key in (
        'text', 'employee_labels', 'source_ids')}, ensure_ascii=False)
    with tempfile.TemporaryDirectory(dir=workdir) as directory:
        folder = Path(directory)
        schema = folder / 'schema.json'
        schema.write_text(json.dumps(SCHEMA), encoding='utf-8')
        answer = folder / 'answer.json'
        if kind == 'codex_cli':
            args = (command or ['codex']) + ['exec', *(['--model', model] if model != 'default' else []),
                '--sandbox', 'read-only', '--skip-git-repo-check', '--ephemeral',
                '-c', 'features.shell_tool=false', '-c', 'web_search="disabled"',
                '-c', 'approval_policy="never"',
                '--output-schema', str(schema), '--output-last-message', str(answer),
                '--color', 'never', '-']
        else:
            args = (command or ['claude']) + ['--bare', '--print', '--model', model,
                '--tools', '', '--disallowedTools', 'mcp__*', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}', '--no-session-persistence',
                '--output-format', 'json', '--json-schema', json.dumps(SCHEMA)]
        try:
            result = subprocess.run(args, input=prompt, capture_output=True,
                encoding='utf-8', timeout=timeout, cwd=folder, shell=False)
        except subprocess.TimeoutExpired:
            raise ValueError('CLI-анализатор превысил время ожидания') from None
        except OSError:
            raise ValueError('Исполняемый файл CLI-анализатора недоступен') from None
        if result.returncode:
            raise ValueError('CLI-анализатор завершился с ошибкой')
        try:
            if kind == 'codex_cli':
                if answer.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError('Слишком большой ответ')
                response = json.loads(answer.read_text(encoding='utf-8'))
            else:
                if len(result.stdout) > 2 * 1024 * 1024:
                    raise ValueError('Слишком большой ответ')
                envelope = json.loads(result.stdout)
                if (not isinstance(envelope, dict) or envelope.get('type') != 'result'
                        or envelope.get('subtype') != 'success' or envelope.get('is_error') is not False):
                    raise ValueError('Незавершённый ответ')
                response = envelope['structured_output']
        except (OSError, KeyError, TypeError, ValueError):
            raise ValueError('CLI-анализатор не вернул завершённый структурированный ответ') from None
        return validate_findings(response, bundle)


def main():
    try:
        if (sys.platform != 'linux' or not Path('/.dockerenv').exists()
                or {path.name for path in Path('/sys/class/net').iterdir()} != {'lo'}):
            raise ValueError('Worker requires a network-disabled Linux container')
        from .gateway import start_local_bridge
        data = json.loads(sys.stdin.read(1024 * 1024))
        validate_handoff(data['bundle'])
        bridge = start_local_bridge('/gateway/provider.sock')
        proxy = f'http://127.0.0.1:{bridge.server_address[1]}'
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            os.environ[key] = proxy
        os.environ['NO_PROXY'] = ''
        try:
            result = run_cli(data['bundle'], data['kind'], data['model'], data['timeout_seconds'], workdir='/work')
            print(json.dumps(result, ensure_ascii=False))
        finally:
            bridge.shutdown()
            bridge.server_close()
        return 0
    except (ValueError, KeyError, TypeError, OSError):
        print('CLI worker stopped', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
