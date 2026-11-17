"""Network-disabled Codex worker; only sanitized handoffs arrive over a Unix socket."""
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys

from .cli_worker import run_cli
from .client import validate_handoff
from .gateway import start_local_bridge


def execute(bundle):
    validate_handoff(bundle)
    model = os.environ.get('CODEX_MODEL', 'default')
    if not model:
        raise ValueError('Разработчик не указал CODEX_MODEL')
    timeout = int(os.environ.get('CODEX_TIMEOUT', '240'))
    if not 1 <= timeout <= 1800:
        raise ValueError('Неверное время ожидания')
    return run_cli(bundle, 'codex_cli', model, timeout, workdir='/work')


def main():
    if (sys.platform != 'linux' or not Path('/.dockerenv').exists()
            or {p.name for p in Path('/sys/class/net').iterdir()} != {'lo'}):
        print('Codex worker requires a network-disabled Linux container', file=sys.stderr)
        return 1
    bridge = start_local_bridge('/gateway/provider.sock')
    proxy = f'http://127.0.0.1:{bridge.server_address[1]}'
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
        os.environ[key] = proxy
    os.environ['NO_PROXY'] = ''
    try:
        if '--login' in sys.argv:
            return subprocess.call(['codex', '-c', 'cli_auth_credentials_store="file"', 'login', '--device-auth'])
        if '--login-status' in sys.argv:
            return subprocess.call(['codex', 'login', 'status'])
        path = '/codex/codex.sock'
        if os.path.exists(path):
            os.unlink(path)
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(30)
                try:
                    raw = self.rfile.readline(1024 * 1024 + 1)
                    if not raw:
                        return  # Socket readiness probe; no analysis or error.
                    if len(raw) > 1024 * 1024 or not raw.endswith(b'\n'):
                        raise ValueError('Invalid handoff size')
                    result = execute(json.loads(raw))
                    response = {'result': result}
                    print(json.dumps({'stage': 'codex', 'status': 'succeeded'}), flush=True)
                except (ValueError, KeyError, TypeError, OSError):
                    response = {'error': 'Codex stopped; check authentication, model and timeout'}
                    print(json.dumps({'stage': 'codex', 'status': 'failed'}), flush=True)
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b'\n')
        class Server(socketserver.UnixStreamServer):
            def handle_error(self, request, client_address):
                print('{"stage":"codex_transport","status":"failed"}', flush=True)
        with Server(path, Handler) as server:
            os.chmod(path, 0o666)
            print('{"stage":"codex_ready","status":"succeeded"}', flush=True)
            server.serve_forever(poll_interval=0.2)
    finally:
        bridge.shutdown()
        bridge.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
