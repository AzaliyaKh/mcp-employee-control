"""SQLite storage used by the local commands."""

import csv
import json
from pathlib import Path
import sqlite3


FIELDS = (
    'full_name', 'aliases', 'bitrix_id', 'kaiten_id', 'gitlab_username',
    'gitlab_emails', 'emails', 'phones', 'logins',
)
LIST_FIELDS = {'aliases', 'gitlab_emails', 'emails', 'phones', 'logins'}


def validate(data):
    if not data['full_name'].strip():
        raise ValueError('ФИО обязательно')
    if not any(data[key] for key in (
            'bitrix_id', 'kaiten_id', 'gitlab_username', 'gitlab_emails')):
        raise ValueError('Укажите хотя бы один аккаунт Bitrix, Kaiten или GitLab')
    for field in ('bitrix_id', 'kaiten_id'):
        if data[field] and (not data[field].isascii() or not data[field].isdigit()
                            or int(data[field]) <= 0):
            raise ValueError(f'{field}: ID должен быть положительным целым числом')


def parse_fields(raw):
    return {field: (list(dict.fromkeys(
                part.strip() for part in raw.get(field, '').split('|') if part.strip()))
            if field in LIST_FIELDS else raw.get(field, '').strip())
            for field in FIELDS}


def read_csv(path):
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream, strict=True)
        if not reader.fieldnames or 'full_name' not in reader.fieldnames:
            raise ValueError('CSV должен содержать столбец full_name')
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError('CSV содержит повторяющиеся имена столбцов')
        unknown = set(reader.fieldnames) - set(FIELDS)
        if unknown:
            raise ValueError('Неизвестные столбцы CSV: ' + ', '.join(sorted(unknown)))
        rows = []
        for number, raw in enumerate(reader, start=2):
            if None in raw or any(value is None for value in raw.values()):
                raise ValueError(f'Строка {number}: неверное число столбцов')
            data = parse_fields(raw)
            rows.append(data)
        return rows


def account_keys(data):
    for field in ('bitrix_id', 'kaiten_id', 'gitlab_username', 'gitlab_emails'):
        values = data[field] if field in LIST_FIELDS else [data[field]]
        for value in values:
            if value:
                normalized = value.strip().casefold()
                if field in ('bitrix_id', 'kaiten_id'):
                    normalized = str(int(normalized))
                yield field, normalized


class Registry:
    def __init__(self, path, read_only=False):
        if read_only:
            self.db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                                      uri=True, timeout=10)
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute('PRAGMA foreign_keys = ON')
        self.db.execute('''CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS accounts (
            kind TEXT NOT NULL, value TEXT NOT NULL,
            employee_id INTEGER NOT NULL REFERENCES employees(id),
            PRIMARY KEY(kind, value))''')
        self.db.commit()

    def close(self):
        self.db.close()

    def add(self, data):
        validate(data)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            cursor = self.db.execute(
                'INSERT INTO employees(data) VALUES (?)',
                (json.dumps(data, ensure_ascii=False),),
            )
            self._claim_accounts(cursor.lastrowid, data)
        return f'EMP_{cursor.lastrowid:03d}'

    def _claim_accounts(self, employee_id, data):
        for kind, value in account_keys(data):
            owner = self.db.execute(
                'SELECT employee_id FROM accounts WHERE kind = ? AND value = ?',
                (kind, value),
            ).fetchone()
            if owner and owner[0] != employee_id:
                raise ValueError(f'Аккаунт {kind}={value} уже принадлежит '
                                 f'EMP_{owner[0]:03d}')
            self.db.execute(
                'INSERT OR IGNORE INTO accounts VALUES (?, ?, ?)',
                (kind, value, employee_id),
            )

    def list(self):
        return [dict(json.loads(data), label=f'EMP_{ident:03d}')
                for ident, data in self.db.execute(
                    'SELECT id, data FROM employees ORDER BY id')]

    def get(self, label):
        for record in self.list():
            if record['label'] == label:
                return record
        raise ValueError(f'Сотрудник {label} не найден')

    def edit(self, label, data, expected):
        validate(data)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old = self.get(label)
            if old != expected:
                raise ValueError('Данные уже изменены другой командой. '
                                 'Повторно откройте редактирование.')
            history = old.get('history', {})
            for field in FIELDS:
                if old[field] != data[field]:
                    values = old[field] if field in LIST_FIELDS else [old[field]]
                    history[field] = list(dict.fromkeys(
                        history.get(field, []) + [value for value in values if value]))
            data = dict(data, history=history)
            self._claim_accounts(int(label[4:]), data)
            self.db.execute('UPDATE employees SET data = ? WHERE id = ?',
                            (json.dumps(data, ensure_ascii=False), int(label[4:])))

    def import_rows(self, rows):
        labels = []
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            for data in rows:
                validate(data)
                cursor = self.db.execute('INSERT INTO employees(data) VALUES (?)',
                                         (json.dumps(data, ensure_ascii=False),))
                self._claim_accounts(cursor.lastrowid, data)
                labels.append(f'EMP_{cursor.lastrowid:03d}')
        return labels

    def preview_import(self, rows):
        new, existing, errors = [], [], []
        claimed = {}
        records = {record['label']: record for record in self.list()}
        for number, data in enumerate(rows, start=2):
            try:
                validate(data)
                owners = set()
                for key in account_keys(data):
                    owner = self.db.execute(
                        'SELECT employee_id FROM accounts WHERE kind = ? AND value = ?',
                        key,
                    ).fetchone()
                    if owner:
                        owners.add(f'EMP_{owner[0]:03d}')
                    if key in claimed:
                        raise ValueError(f'Аккаунт повторяется в CSV: строка {claimed[key]}')
                if owners:
                    if len(owners) != 1:
                        raise ValueError('Аккаунты принадлежат разным сотрудникам: '
                                         + ', '.join(sorted(owners)))
                    label = owners.pop()
                    record = records[label]
                    names = [record['full_name'], *record['aliases'],
                             *record.get('history', {}).get('full_name', [])]
                    if data['full_name'].casefold() not in {name.casefold() for name in names}:
                        raise ValueError(f'Аккаунт уже принадлежит {label}, ФИО отличается')
                    existing.append((number, label, data['full_name']))
                else:
                    new.append(data)
                    for key in account_keys(data):
                        claimed[key] = number
            except ValueError as error:
                errors.append(f'Строка {number}: {error}')
        return new, existing, errors
