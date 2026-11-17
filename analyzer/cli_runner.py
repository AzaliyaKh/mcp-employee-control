"""Trusted host launcher. Never give the application container a Docker socket."""
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .client import validate_handoff


def docker_executable():
    executable = shutil.which('docker')
    if executable:
        return executable
    for root, suffix in (
            ('LOCALAPPDATA', 'Programs/DockerDesktop/resources/bin/docker.exe'),
            ('ProgramFiles', 'Docker/Docker/resources/bin/docker.exe')):
        folder = os.environ.get(root)
        if folder:
            candidate = Path(folder) / suffix
            if candidate.is_file():
                return str(candidate)
    return 'docker'  # The caller converts an unavailable executable to a safe error.


def run_container(bundle, config):
    validate_handoff(bundle)
    kind = config.get('kind')
    key = {'codex_cli': 'CODEX_API_KEY', 'claude_code': 'ANTHROPIC_API_KEY'}.get(kind)
    model, image, volume = (config.get(field) for field in ('model', 'image', 'gateway_volume'))
    timeout = config.get('timeout_seconds', 300)
    if (key is None or not isinstance(model, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,150}', model)
            or not isinstance(image, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,250}', image)
            or not isinstance(volume, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', volume)
            or type(timeout) is not int or not 1 <= timeout <= 1800):
        raise ValueError('Неверная конфигурация CLI-анализатора')
    secret = os.environ.get(key, '')
    if not secret:
        raise ValueError('Не настроен ' + key)
    # Docker CLI receives only its OS essentials and the selected provider key.
    env = {name: os.environ[name] for name in (
        'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'HOME', 'USERPROFILE') if name in os.environ}
    env[key] = secret
    docker = docker_executable()
    name = 'worklog-analysis-' + uuid.uuid4().hex
    args = [docker, 'run', '--rm', '-i', '--pull=never', '--name', name,
            '--network=none', '--read-only', '--cap-drop=ALL',
            '--security-opt=no-new-privileges', '--user=65532:65532',
            '--memory=1g', '--cpus=1', '--pids-limit=128', '--log-driver=none',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=268435456,mode=1777',
            '--tmpfs', '/work:rw,nosuid,nodev,size=268435456,mode=1777',
            '--mount', f'type=volume,source={volume},target=/gateway,readonly',
            '--env', key, '--env', 'HOME=/work', '--env', 'CODEX_HOME=/work/.codex',
            '--env', 'CLAUDE_CONFIG_DIR=/work/.claude', '--workdir=/work',
            '--entrypoint=python', image, '-m', 'analyzer.cli_worker']
    payload = json.dumps({'bundle': bundle, 'kind': kind, 'model': model,
                          'timeout_seconds': timeout}, ensure_ascii=False)
    try:
        result = subprocess.run(args, input=payload, capture_output=True, encoding='utf-8',
                                timeout=timeout + 15, env=env, shell=False)
        if result.returncode:
            raise ValueError('CLI-анализатор остановлен; проверьте образ, шлюз и доступ провайдера')
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('Ответ CLI-анализатора слишком большой')
        try:
            return json.loads(result.stdout)
        except (ValueError, TypeError):
            raise ValueError('CLI-анализатор не вернул JSON') from None
    except subprocess.TimeoutExpired:
        raise ValueError('Превышено время работы CLI-анализатора') from None
    except OSError:
        raise ValueError('Docker недоступен; локальный запуск без изоляции запрещён') from None
    finally:
        try:
            subprocess.run([docker, 'rm', '-f', name], capture_output=True,
                           timeout=10, env={k: v for k, v in env.items() if k != key}, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
