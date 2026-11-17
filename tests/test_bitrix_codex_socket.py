import hashlib
import json
import os
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from analyzer.codex_daemon import main
from analyzer.socket_client import analyze_socket
from analyzer.client import validate_handoff
from analyzer.gateway import connect_provider


class CodexBoundaries(unittest.TestCase):
    def test_daemon_refuses_host_execution(self):
        with patch('analyzer.codex_daemon.sys.platform', 'win32'), patch('sys.stderr'):
            self.assertEqual(main(), 1)

    def test_subscription_gateway_never_resolves_unapproved_hosts(self):
        hosts = ('api.openai.com', 'chatgpt.com', 'auth.openai.com')
        for target in ('127.0.0.1:443', 'bitrix.example.test:443', 'chatgpt.com:80', 'chatgpt.com.evil.test:443'):
            with self.subTest(target=target), patch('analyzer.gateway.socket.getaddrinfo') as resolve:
                with self.assertRaises(ValueError):
                    connect_provider(target, hosts)
                resolve.assert_not_called()


@unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'Unix sockets require Linux container in this Python build')
class CodexSocket(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'codex.sock')
        self.bundle = {'protocol': 1, 'status': 'policy_checked', 'text': 'EMP_001: result',
            'version': hashlib.sha256(b'EMP_001: result').hexdigest(),
            'employee_labels': ['EMP_001'], 'source_ids': ['bitrix:1782']}
        self.seen = []
        owner = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                data = json.loads(self.rfile.readline())
                validate_handoff(data)
                owner.seen.append(data)
                self.wfile.write(json.dumps(owner.response).encode() + b'\n')
        self.response = {'result': {'summary': 'EMP_001 completed', 'findings': [
            {'claim': 'result', 'status': 'confirmed', 'sources': ['bitrix:1782']}]}}
        server = socketserver.UnixStreamServer(self.path, Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)

    def test_checked_bundle_crosses_actual_unix_socket_without_local_mapping(self):
        result = analyze_socket(self.bundle, {'kind': 'codex_socket', 'socket': self.path, 'timeout_seconds': 3})
        self.assertEqual(result['summary'], 'EMP_001 completed')
        self.assertEqual(self.seen, [self.bundle])
        self.assertNotIn('names', self.seen[0])

    def test_socket_error_does_not_return_fake_success(self):
        self.response = {'error': 'not authenticated'}
        with self.assertRaises(ValueError):
            analyze_socket(self.bundle, {'kind': 'codex_socket', 'socket': self.path, 'timeout_seconds': 3})

    def test_unknown_source_in_socket_response_is_rejected(self):
        self.response['result']['findings'][0]['sources'] = ['unknown']
        with self.assertRaises(ValueError):
            analyze_socket(self.bundle, {'kind': 'codex_socket', 'socket': self.path, 'timeout_seconds': 3})


if __name__ == '__main__':
    unittest.main()
