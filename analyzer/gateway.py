"""CONNECT-only provider gateway and container-local Unix socket bridge.

The worker has --network=none. Its only egress is this dedicated Unix socket.
TLS remains end-to-end between the CLI and the provider.
"""
import ipaddress
import os
import select
import socket
import socketserver
import sys
import threading
import time


PROVIDERS = {'codex_cli': 'api.openai.com', 'claude_code': 'api.anthropic.com'}
PROVIDERS['codex_subscription'] = ('api.openai.com', 'chatgpt.com', 'auth.openai.com')


def connect_provider(target, allowed_host):
    allowed = (allowed_host,) if isinstance(allowed_host, str) else allowed_host
    matches = [host for host in allowed if target.casefold() == host + ':443']
    if not matches:
        raise ValueError('Destination is not allowed')
    allowed_host = matches[0]
    addresses = socket.getaddrinfo(allowed_host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global
                            or ipaddress.ip_address(item[4][0]).is_multicast for item in addresses):
        raise ValueError('Non-public provider address')
    # Connect to the validated numeric address; do not resolve a second time.
    for family, socktype, proto, _, address in addresses:
        connection = socket.socket(family, socktype, proto)
        try:
            connection.settimeout(15)
            connection.connect(address)
            return connection
        except OSError:
            connection.close()
    raise OSError('Provider unavailable')


def relay(left, right, timeout=1800):
    deadline, transferred = time.monotonic() + timeout, 0
    while time.monotonic() < deadline:
        readable, _, _ = select.select([left, right], [], [], min(10, max(0, deadline - time.monotonic())))
        for source in readable:
            data = source.recv(65536)
            if not data:
                return
            transferred += len(data)
            if transferred > 64 * 1024 * 1024:
                return
            destination = right if source is left else left
            destination.sendall(data)


def handle_connect(client, allowed_host):
    client.settimeout(15)
    header = bytearray()
    while not header.endswith(b'\r\n\r\n'):
        chunk = client.recv(1)
        if not chunk or len(header) >= 16384:
            raise ValueError('Invalid CONNECT header')
        header.extend(chunk)
    line = bytes(header).split(b'\r\n', 1)[0].decode('ascii')
    method, target, version = line.split(' ')
    if method != 'CONNECT' or version not in ('HTTP/1.0', 'HTTP/1.1'):
        raise ValueError('Only CONNECT is supported')
    with connect_provider(target, allowed_host) as upstream:
        client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        relay(client, upstream)


class QuietServerMixin:
    daemon_threads = True
    def handle_error(self, request, client_address):
        pass  # Never log requests, source fragments or credentials.


def start_local_bridge(path):
    class Bridge(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                    upstream.settimeout(15)
                    upstream.connect(path)
                    relay(self.request, upstream)
            except OSError:
                pass
    class Server(QuietServerMixin, socketserver.ThreadingTCPServer):
        allow_reuse_address = True
    server = Server(('127.0.0.1', 0), Bridge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    # This entry point is only for the trusted gateway container, never the worker.
    allowed = PROVIDERS[sys.argv[1]]
    path = '/gateway/provider.sock'
    if os.path.exists(path):
        os.unlink(path)
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                handle_connect(self.request, allowed)
            except (OSError, ValueError):
                try:
                    self.request.sendall(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                except OSError:
                    pass
    class Server(QuietServerMixin, socketserver.ThreadingUnixStreamServer):
        pass
    with Server(path, Handler) as server:
        os.chmod(path, 0o666)
        server.serve_forever()


if __name__ == '__main__':
    main()
