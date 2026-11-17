import argparse
import json
import os
import sqlite3
import sys

from . import runs


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальная маскировка текста')
    parser.add_argument('--db', default=os.environ.get(
        'EMPLOYEES_DB', 'data/private/registry.sqlite'))
    sub = parser.add_subparsers(dest='command', required=True)
    preparing = sub.add_parser('prepare', help='Подготовить текст локально')
    preparing.add_argument('file')
    preparing.add_argument('--run-dir', required=True)
    inspecting = sub.add_parser('inspect', help='Показать состояние и найденные данные локально')
    inspecting.add_argument('run_dir')
    deciding = sub.add_parser('decide', help='Разрешить передачу показанной версии текста')
    deciding.add_argument('run_dir')
    deciding.add_argument('action', choices=['continue', 'skip'])
    deciding.add_argument('--version', required=True, help='Версия из prepare/inspect')
    exporting = sub.add_parser('export', help='Сохранить разрешённый текст для анализа')
    exporting.add_argument('run_dir')
    exporting.add_argument('--output', required=True)
    exporting.add_argument('--bundle', action='store_true', help='Конверт для анализатора без локального маппинга')
    restoring = sub.add_parser('restore', help='Восстановить имена в готовом ответе')
    restoring.add_argument('run_dir')
    restoring.add_argument('answer')
    restoring.add_argument('--output', required=True)
    restoring.add_argument('--json', action='store_true', help='Восстановить имена в JSON без нарушения экранирования')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = runs.prepare(args.file, args.db, args.run_dir)
        elif args.command == 'inspect':
            state, _ = runs.load(args.run_dir)
            result = runs.public_state(state)
        elif args.command == 'decide':
            result = runs.decide(args.run_dir, args.action, args.version)
        elif args.command == 'export':
            result = runs.export(args.run_dir, args.output, bundle=args.bundle)
        else:
            result = runs.restore(args.run_dir, args.answer, args.output, json_output=args.json)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Операция прервана.', file=sys.stderr)
        return 130
