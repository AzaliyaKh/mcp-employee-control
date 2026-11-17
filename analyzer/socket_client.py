"""Only checked materials cross the isolated analyzer's Unix socket."""
import json
import socket
from .client import validate_handoff, validate_findings

MAX_MESSAGE = 2 * 1024 * 1024


def analyze_socket(bundle, config):
    validate_handoff(bundle)
    if config.get('kind') != 'codex_socket':
        raise ValueError('Для этой цепочки требуется codex_socket')
    timeout = config.get('timeout_seconds', 300)
    if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
        raise ValueError('Некорректное время ожидания Codex')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout + 15)
            connection.connect(config.get('socket', '/codex/codex.sock'))
            connection.sendall(json.dumps(bundle, ensure_ascii=False).encode() + b'\n')
            with connection.makefile('rb') as stream:
                raw = stream.readline(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                raise ValueError('Некорректный размер ответа Codex')
            response = json.loads(raw)
            if set(response) != {'result'}:
                raise ValueError('Codex остановлен: проверьте вход, модель и журнал контейнера')
            return validate_findings(response['result'], bundle)
    except (OSError, AttributeError):
        raise ValueError('Изолированный Codex недоступен или истекло время ожидания') from None
