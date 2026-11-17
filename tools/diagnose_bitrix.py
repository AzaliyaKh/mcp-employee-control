"""Run read-only diagnostics inside the existing service; no image rebuild needed."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analyzer.cli_runner import docker_executable


def main():
    ids = sys.argv[1:]
    if not ids or len(ids) > 10 or any(not x.isdigit() or int(x) < 1 for x in ids):
        print('Пример: python tools/diagnose_bitrix.py 54 100 1782')
        return 1
    source = (ROOT / 'integrations/bitrix_diagnostics.py').read_text(encoding='utf-8')
    try:
        result = subprocess.run([docker_executable(), 'exec', '-i', 'ai-service-1', 'python', '-', *ids],
            input=source, capture_output=True, encoding='utf-8', timeout=300, shell=False)
        print(result.stdout)
        if result.returncode:
            print('Диагностика не завершена. Проверьте запущенный ai-service-1; секреты присылать не нужно.')
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        print('Нет доступа к Docker или истекло время ожидания. Запустите команду в обычном PowerShell.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
