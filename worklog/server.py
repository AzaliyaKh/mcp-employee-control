"""Loopback-only HTTP interface with a single background queue worker."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import html
import json
import os
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .application import Application


def serve(data_dir, config, port, host='127.0.0.1'):
    if host not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('Неверный адрес HTTP-сервиса')
    token = os.environ.get('WORKLOG_TOKEN', '')
    if os.environ.get('WORKLOG_TOKEN_FILE'):
        from pathlib import Path
        token = Path(os.environ['WORKLOG_TOKEN_FILE']).read_text(encoding='utf-8').strip()
    if len(token) < 16:
        raise ValueError('WORKLOG_TOKEN должен содержать не менее 16 символов')
    with_application = lambda: Application(data_dir, config)
    initialized = with_application()
    initialized.recover_interrupted()
    initialized.close()
    stopping = threading.Event()

    def worker():
        app = with_application()
        try:
            while not stopping.is_set():
                row = app.db.execute("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    app.process(row['id'])
                else:
                    stopping.wait(0.1)
        finally:
            app.close()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'WorklogLocal'

        def log_message(self, *args):
            # Request paths and headers must not leak into a developer log.
            pass

        def send_json(self, code, value):
            payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

        def authorized(self):
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode('utf-8'), ('Bearer ' + token).encode('utf-8')):
                self.send_json(401, {'error': 'Требуется локальный токен подключения'})
                return False
            if self.headers.get('Origin'):
                self.send_json(403, {'error': 'Запросы из браузерных страниц не разрешены'})
                return False
            return True

        def report_link(self, result):
            if result.get('report_id'):
                ident = result['report_id']
                expires = str(int(time.time()) + 600)
                signature = hmac.digest(token.encode(), f'{ident}:{expires}'.encode(), 'sha256').hex()
                public = os.environ.get('WORKLOG_PUBLIC_URL', f'http://127.0.0.1:{self.server.server_port}').rstrip('/')
                result['report_url'] = (f'{public}/view/{ident}'
                                        f'?expires={expires}&ticket={signature}')
            return result

        def view_report(self, path):
            ident = path.removeprefix('/view/')
            query = parse_qs(urlsplit(self.path).query)
            try:
                if set(query) != {'expires', 'ticket'} or any(len(values) != 1 for values in query.values()):
                    raise ValueError('Неверная ссылка')
                expires, ticket = query['expires'][0], query['ticket'][0]
                expected = hmac.digest(token.encode(), f'{ident}:{expires}'.encode(), 'sha256').hex()
                if not int(time.time()) <= int(expires) <= int(time.time()) + 600:
                    raise ValueError('Ссылка истекла')
                if not hmac.compare_digest(ticket.encode(), expected.encode()):
                    raise ValueError('Неверная подпись')
            except (KeyError, ValueError):
                self.send_json(401, {'error': 'Ссылка на отчёт недействительна или истекла'})
                return
            app = with_application()
            try:
                report = app.report(ident)
                body = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                        '<title>Отчёт о работе</title><h1>Отчёт о работе</h1><pre>'
                        + html.escape(json.dumps(report, ensure_ascii=False, indent=2))
                        + '</pre></html>').encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'")
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, OSError, sqlite3.Error):
                self.send_json(404, {'error': 'Отчёт не найден'})
            finally:
                app.close()

        def do_GET(self):
            if urlsplit(self.path).path.startswith('/view/'):
                self.view_report(urlsplit(self.path).path)
                return
            if not self.authorized():
                return
            app = with_application()
            try:
                path = urlsplit(self.path).path
                if path == '/health':
                    self.send_json(200, {'status': 'ok'})
                elif path == '/employees':
                    query = parse_qs(urlsplit(self.path).query)
                    if set(query) != {'query'} or len(query['query']) != 1:
                        raise ValueError('Требуется query')
                    self.send_json(200, app.resolve_employee(query['query'][0]))
                elif path.startswith('/analysis/'):
                    self.send_json(200, self.report_link(app.status(path.removeprefix('/analysis/'))))
                elif path.startswith('/reports/'):
                    self.send_json(200, app.report(path.removeprefix('/reports/')))
                else:
                    self.send_json(404, {'error': 'Маршрут не найден'})
            except (ValueError, OSError, sqlite3.Error):
                self.send_json(404, {'error': 'Запуск или готовый отчёт не найден'})
            finally:
                app.close()

        def do_POST(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            decision = path.startswith('/privacy/')
            if path != '/analysis' and not decision:
                self.send_json(404, {'error': 'Маршрут не найден'})
                return
            app = None
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length < 1 or length > 65536 or self.headers.get('Transfer-Encoding'):
                    raise ValueError('Неверная длина запроса')
                data = json.loads(self.rfile.read(length))
                if decision:
                    fields = {'task_id', 'version', 'action'}
                    if (not isinstance(data, dict) or not fields <= data.keys()
                            or set(data) - fields - {'cleaned_text'}
                            or type(data['task_id']) is not int
                            or not isinstance(data['version'], str) or not isinstance(data['action'], str)):
                        raise ValueError('Неверное решение')
                    app = with_application()
                    result = app.decide_privacy(path.removeprefix('/privacy/'), **data)
                    self.send_json(202, result)
                    return
                fields = {'kind', 'employee_id', 'start', 'end'}
                if (not isinstance(data, dict) or set(data) != fields
                        or not all(isinstance(value, str) for value in data.values())
                        or data['kind'] not in ('employee_statistics', 'employee_review')):
                    raise ValueError('Неверные параметры запроса статистики')
                app = with_application()
                result = app.start(data['employee_id'], data['start'], data['end'], kind=data['kind'])
                self.send_json(202, result)
            except (ValueError, KeyError, TypeError, OverflowError, OSError, sqlite3.Error):
                self.send_json(400, {'error': 'Неверный запрос или не настроен источник данных'})
            finally:
                if app is not None:
                    app.close()

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(json.dumps({'address': f'http://127.0.0.1:{server.server_port}'}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        server.server_close()
        thread.join(timeout=10)
