"""Start an isolated synthetic HTTP demo; no external API or model requests."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from employees.registry import Registry
from worklog.server import serve


def prepare(folder):
    folder = Path(folder).resolve()
    if not folder.is_relative_to(ROOT / 'data') or folder == ROOT / 'data':
        raise ValueError('Демонстрационные данные должны находиться в отдельной папке внутри data')
    marker = folder / 'demo-marker.json'
    if folder.exists():
        if not marker.is_file() or json.loads(marker.read_text(encoding='utf-8')) != {'demo': 'lmstudio-v1'}:
            raise ValueError('Папка уже существует и не является этой демонстрацией; выберите новую --data-dir')
        token = (folder / 'local-token.txt').read_text(encoding='utf-8').strip()
        if len(token) < 16:
            raise ValueError('Повреждён локальный токен демонстрации')
        return token
    folder.mkdir(parents=True, exist_ok=False)
    registry = Registry(folder / 'private' / 'registry.sqlite')
    try:
        registry.add({'full_name': 'Иванов Иван (демо)', 'aliases': ['Демо Иванов'],
                      'bitrix_id': '999001', 'kaiten_id': '', 'gitlab_username': 'demo_ivanov',
                      'gitlab_emails': ['demo-ivanov@example.test'], 'emails': [], 'phones': [], 'logins': []})
    finally:
        registry.close()
    tasks = json.loads((ROOT / 'examples' / 'tasks.json').read_text(encoding='utf-8'))
    (folder / 'tasks.json').write_text(json.dumps(tasks, ensure_ascii=False), encoding='utf-8')
    (folder / 'config.json').write_text(json.dumps({'demo': True, 'source_file': 'tasks.json'}), encoding='utf-8')
    token = secrets.token_urlsafe(32)
    (folder / 'local-token.txt').write_text(token, encoding='utf-8')
    marker.write_text(json.dumps({'demo': 'lmstudio-v1'}), encoding='utf-8')
    return token


def main():
    parser = argparse.ArgumentParser(description='Локальный HTTP-сервис с вымышленным сотрудником для LM Studio')
    parser.add_argument('--data-dir', default='data/lmstudio-demo')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    folder = (ROOT / args.data_dir).resolve()
    try:
        token = prepare(folder)
        print(f'Адрес: http://127.0.0.1:{args.port}', flush=True)
        print(f'Файл токена для настроек плагина: {folder / "local-token.txt"}', flush=True)
        print('Данные: только Иванов Иван (демо), октябрь 2026. Ожидается: выполнено 1, просрочено 1, текущих 0.', flush=True)
        if args.prepare_only:
            return 0
        os.environ['WORKLOG_TOKEN'] = token
        serve(folder, folder / 'config.json', args.port)
        return 0
    except KeyboardInterrupt:
        print('Демонстрационный сервис остановлен.')
        return 0
    except (ValueError, OSError, KeyError) as error:
        print(f'Не удалось запустить демонстрацию: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
