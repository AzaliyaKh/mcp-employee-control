"""Prepare only project-local live settings, using an explicit existing registry."""
import argparse
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from employees.registry import Registry


def prepare(registry_path):
    source = Path(registry_path).resolve()
    if not source.is_relative_to(ROOT / 'data') or not source.is_file():
        raise ValueError('Укажите существующий справочник внутри data')
    node = shutil.which('node')
    if not node:
        raise ValueError('Node.js не найден')
    check = Registry(source, read_only=True)
    try:
        if not check.list():
            raise ValueError('Справочник пуст')
    finally:
        check.close()
    folder = ROOT / 'data' / 'bitrix-live'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'private' / 'registry.sqlite'
    target.parent.mkdir(exist_ok=True)
    if source != target and not target.exists():
        # SQLite backup includes committed WAL data; the source is opened read-only.
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as origin, sqlite3.connect(target) as destination:
            origin.backup(destination)
    files = {
        'config.json': {'source': 'bitrix', 'bitrix': {'base_env': 'BITRIX_WEBHOOK_URL', 'max_tasks': 1000},
                        'analyzer': {'kind': 'codex_socket', 'socket': '/codex/codex.sock', 'timeout_seconds': 260}},
        'mcp-entry.json': {'mcpServers': {'employee-worklog': {'command': node, 'args': [
            str(ROOT / 'mcp-bridge' / 'server.mjs'), '--address', 'http://127.0.0.1:8000',
            '--token-file', str(folder / 'local-token.txt'), '--enable-privacy']}}},
    }
    for name, value in files.items():
        path = folder / name
        if not path.exists():
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    token = folder / 'local-token.txt'
    if not token.exists():
        token.write_text(secrets.token_urlsafe(32), encoding='utf-8')
    print('Настройки: data/bitrix-live/config.json')
    print('MCP: data/bitrix-live/mcp-entry.json (обновите прежнее подключение)')
    print('Рабочий справочник: data/bitrix-live/private/registry.sqlite')
    print('Существующие файлы не перезаписываются. Исходный справочник не изменён.')


def main():
    parser = argparse.ArgumentParser(description='Подготовка реальной цепочки Bitrix → Codex')
    parser.add_argument('--registry', required=True)
    args = parser.parse_args()
    try:
        prepare(args.registry)
        return 0
    except (ValueError, OSError, sqlite3.Error):
        print('Не удалось подготовить настройки: проверьте путь к справочнику, его содержимое и права.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
