"""Save bounded Docker service logs without PowerShell's stderr formatting."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyzer.cli_runner import docker_executable


def main():
    folder = ROOT / 'data' / 'bitrix-live'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run([docker_executable(), 'compose', '-f', 'compose.bitrix.yaml',
            'logs', '--no-color', '--timestamps', '--tail', '5000', 'service', 'codex'], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30, shell=False)
        path = folder / 'docker.log'
        path.write_bytes(result.stdout)
        print('Лог сохранён: data/bitrix-live/docker.log')
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        print('Не удалось получить Docker-логи: проверьте Docker Desktop.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
