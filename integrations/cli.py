import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from employees.registry import Registry
from .sources import Bitrix, GitLab, Kaiten


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальная выгрузка напрямую из систем-источников')
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    bitrix = sub.add_parser('bitrix-task')
    bitrix.add_argument('task_id', type=int)
    bitrix.add_argument('--output', required=True)
    kaiten = sub.add_parser('kaiten-card')
    kaiten.add_argument('task_id', type=int)
    kaiten.add_argument('--output', required=True)
    gitlab = sub.add_parser('gitlab-changes')
    gitlab.add_argument('--db', default=os.environ.get('EMPLOYEES_DB', 'data/private/registry.sqlite'))
    gitlab.add_argument('--employee', required=True)
    gitlab.add_argument('--project', required=True)
    gitlab.add_argument('--since', required=True)
    gitlab.add_argument('--until', required=True)
    gitlab.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        output = Path(args.output)
        if output.exists():
            raise ValueError('Выходной файл уже существует; перезапись запрещена')
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        source_name = {'bitrix-task': 'bitrix', 'kaiten-card': 'kaiten',
                       'gitlab-changes': 'gitlab'}[args.command]
        entry = config[source_name]
        base = os.environ.get(entry.get('base_env', ''), '') or entry.get('base_url', '')
        if hasattr(args, 'task_id') and args.task_id <= 0:
            raise ValueError('Номер задачи должен быть положительным')
        if source_name == 'bitrix':
            result = Bitrix(base).task(args.task_id)
        else:
            token = os.environ.get(entry.get('token_env', ''), '')
            if not token:
                raise ValueError('Не настроен токен источника')
            if source_name == 'kaiten':
                result = Kaiten(base, token, entry.get('report_property_id')).card(args.task_id)
            else:
                registry = Registry(args.db, read_only=True)
                try:
                    person = registry.get(args.employee)
                finally:
                    registry.close()
                result = GitLab(base, token).changes(args.project, person, args.since, args.until)
        with output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'status': 'collected_locally', 'source': result['source'],
                          'masked': False}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        # Source responses, configured URLs and secrets never appear in diagnostics.
        print(f'Ошибка сбора: {error if isinstance(error, ValueError) else type(error).__name__}',
              file=sys.stderr)
        return 1
