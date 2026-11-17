import argparse
import csv
import json
import os
import sqlite3
import sys

from .registry import FIELDS, LIST_FIELDS, Registry, read_csv


PROMPTS = {
    'full_name': 'ФИО', 'aliases': 'Варианты имени (через |)',
    'bitrix_id': 'ID в Bitrix', 'kaiten_id': 'ID в Kaiten',
    'gitlab_username': 'Логин GitLab',
    'gitlab_emails': 'Почты автора коммитов GitLab (через |)',
    'emails': 'Другие почты (через |)', 'phones': 'Телефоны (через |)',
    'logins': 'Другие логины (через |)',
}


def read_fields(current=None):
    result = {}
    for field in FIELDS:
        hint = ''
        if current is not None:
            shown = (' | '.join(current[field]) if field in LIST_FIELDS
                     else current[field])
            hint = f' [{shown}]'
        value = input(PROMPTS[field] + hint + ': ').strip()
        if current is not None and not value:
            result[field] = current[field]
            continue
        if current is not None and value == '-':
            value = ''
        result[field] = ([part.strip() for part in value.split('|') if part.strip()]
                         if field in LIST_FIELDS else value)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='Локальный справочник сотрудников')
    parser.add_argument('--db', default=os.environ.get(
        'EMPLOYEES_DB', 'data/private/registry.sqlite'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('add', help='Добавить сотрудника интерактивно')
    editing = sub.add_parser('edit', help='Изменить данные сотрудника')
    editing.add_argument('label', help='Постоянная метка, например EMP_001')
    importing = sub.add_parser('import', help='Предпросмотр и загрузка CSV')
    importing.add_argument('file', help='Путь к CSV в UTF-8')
    listing = sub.add_parser('list', help='Показать сотрудников')
    listing.add_argument('--json', action='store_true', help='Вывести полные записи JSON')
    args = parser.parse_args(argv)
    registry = None
    try:
        registry = Registry(args.db)
        if args.command == 'add':
            label = registry.add(read_fields())
            print(f'Сотрудник добавлен: {label}')
        elif args.command == 'edit':
            current = registry.get(args.label)
            print('Enter — оставить значение; - — очистить необязательное поле.')
            registry.edit(args.label, read_fields(current), expected=current)
            print(f'Сотрудник обновлён: {args.label}')
        elif args.command == 'import':
            rows = read_csv(args.file)
            rows, existing, errors = registry.preview_import(rows)
            for row in rows:
                print(f"НОВЫЙ: {row['full_name']}")
            for number, label, name in existing:
                print(f'Строка {number}: СУЩЕСТВУЕТ {label} — {name}; без изменений')
            print(f'Новых: {len(rows)}. Существующих: {len(existing)}. Ошибок: {len(errors)}')
            if errors:
                for error in errors:
                    print(error, file=sys.stderr)
                print('Импорт не выполнен: исправьте ошибки CSV.', file=sys.stderr)
                return 1
            if not rows:
                return 0
            if input('Сохранить новых сотрудников? [да/нет]: ').strip().casefold() not in ('да', 'yes', 'y'):
                print('Импорт отменён. Данные не сохранены.')
                return 0
            labels = registry.import_rows(rows)
            print(f"Импортировано: {len(labels)}. Метки: {', '.join(labels)}")
        elif args.command == 'list':
            records = registry.list()
            if args.json:
                print(json.dumps(records, ensure_ascii=False, indent=2))
            elif not records:
                print('Справочник пуст.')
            else:
                for record in records:
                    print(f"{record['label']} | {record['full_name']} | "
                          f"Bitrix: {record['bitrix_id'] or '—'} | "
                          f"Kaiten: {record['kaiten_id'] or '—'} | "
                          f"GitLab: {record['gitlab_username'] or '—'}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nОперация отменена. Данные не сохранены.', file=sys.stderr)
        return 130
    except csv.Error as error:
        print(f'Ошибка CSV: {error}. Данные не сохранены.', file=sys.stderr)
        return 1
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 1
    finally:
        if registry is not None:
            registry.close()
