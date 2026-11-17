import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .statistics import statistics
from .application import Application


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальный расчёт статистики задач')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--config')
    sub = parser.add_subparsers(dest='command', required=True)
    stats = sub.add_parser('statistics', help='Посчитать показатели нормализованной истории')
    stats.add_argument('file')
    stats.add_argument('--employee', required=True)
    stats.add_argument('--start', required=True)
    stats.add_argument('--end', required=True)
    serving = sub.add_parser('serve', help='Запустить локальный HTTP-интерфейс')
    serving.add_argument('--port', type=int, default=8000)
    serving.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1')
    start = sub.add_parser('start', help='Создать запрос статистики')
    start.add_argument('--employee', required=True)
    start.add_argument('--start', required=True)
    start.add_argument('--end', required=True)
    for name in ('process', 'status', 'report'):
        command = sub.add_parser(name)
        command.add_argument('id')
    args = parser.parse_args(argv)
    app = None
    try:
        if args.command == 'serve':
            from .server import serve
            serve(args.data_dir, args.config, args.port, args.host)
            return 0
        if args.command == 'statistics':
            tasks = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            result = statistics(tasks, args.employee, args.start, args.end)
        else:
            app = Application(args.data_dir, args.config)
            if args.command == 'start':
                result = app.start(args.employee, args.start, args.end)
            else:
                result = getattr(app, args.command)(args.id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KeyError, TypeError, OverflowError, OSError, sqlite3.Error) as error:
        print(f'Ошибка входных данных: {error}', file=sys.stderr)
        return 1
    finally:
        if app is not None:
            app.close()
