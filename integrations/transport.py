import json
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class JsonTransport:
    def __init__(self, base_url, headers=None):
        url = urlsplit(base_url)
        if (url.scheme not in ('https', 'http') or not url.hostname
                or url.username or url.password or url.query or url.fragment
                or (url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1'))):
            raise ValueError('Источник должен использовать HTTPS; HTTP допустим только для локальных проверок')
        self.base = base_url.rstrip('/') + '/'
        self.headers = headers or {}
        self.opener = build_opener(NoRedirect(), ProxyHandler({}))

    def request(self, path, body=None, params=None):
        if path.startswith('/') or '://' in path or '..' in path.split('/'):
            raise ValueError('Недопустимый путь запроса')
        address = self.base + path
        if params:
            address += '?' + urlencode(params)
        try:
            request = Request(address, headers={'Accept': 'application/json',
                **self.headers, **({'Content-Type': 'application/json'} if body is not None else {})},
                data=json.dumps(body).encode('utf-8') if body is not None else None)
            with self.opener.open(request, timeout=30) as response:
                payload = response.read(16 * 1024 * 1024 + 1)
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError('Ответ источника превышает допустимый размер')
                return json.loads(payload), dict(response.headers)
        except HTTPError as error:
            error.close()
            raise ValueError(f'Источник вернул HTTP {error.code}') from None
        except (URLError, TimeoutError, OSError):
            raise ValueError('Не удалось подключиться к настроенному источнику') from None
        except (ValueError, HTTPException):
            raise ValueError('Некорректный HTTP-запрос или ответ; проверьте настройки подключения') from None
