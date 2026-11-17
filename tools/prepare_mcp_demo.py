"""Generate a local MCP entry without editing Bionic or exposing the service token."""
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.start_lmstudio_demo import prepare


def configuration(folder, node):
    return {'mcpServers': {'employee-worklog': {'command': str(node), 'args': [
        str(ROOT / 'mcp-bridge' / 'server.mjs'), '--address', 'http://127.0.0.1:8000',
        '--token-file', str(folder / 'local-token.txt')]}}}


def main():
    node = shutil.which('node')
    if not node:
        print('Node.js не найден. Требуется Node.js 22.21 или новее.', file=sys.stderr)
        return 1
    folder = ROOT / 'data' / 'lmstudio-demo'
    try:
        prepare(folder)
        path = folder / 'mcp-entry.json'
        data = configuration(folder, node)
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8')) != data:
                raise ValueError('mcp-entry.json уже существует с другими настройками; файл не перезаписан')
        else:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Конфигурация MCP: ' + str(path))
        print('Добавьте запись employee-worklog в Bionic: Settings -> MCP. В конфигурации нет значения токена.')
        print('HTTP-сервис запускается отдельно: python tools/start_lmstudio_demo.py')
        return 0
    except (OSError, ValueError):
        print('Не удалось подготовить демонстрационную конфигурацию; существующие настройки не изменены.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
