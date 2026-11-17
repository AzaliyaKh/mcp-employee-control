import socket
import threading
import unittest
from unittest.mock import patch

from analyzer.gateway import connect_provider, handle_connect, relay


class GatewayPolicy(unittest.TestCase):
    def test_connect_tunnels_bytes_with_local_synthetic_upstream(self):
        client, gateway = socket.socketpair()
        upstream, provider = socket.socketpair()
        for connection in (client, gateway, upstream, provider):
            connection.settimeout(3)
            self.addCleanup(connection.close)
        failures = []
        def serve():
            try:
                handle_connect(gateway, 'api.openai.com')
            except Exception as error:
                failures.append(error)
        with patch('analyzer.gateway.connect_provider', return_value=upstream) as connect:
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            client.sendall(b'CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com\r\n\r\n')
            self.assertIn(b'200 Connection Established', client.recv(4096))
            client.sendall(b'synthetic TLS request')
            self.assertEqual(provider.recv(4096), b'synthetic TLS request')
            provider.sendall(b'synthetic TLS response')
            self.assertEqual(client.recv(4096), b'synthetic TLS response')
            client.shutdown(socket.SHUT_WR)
            thread.join(timeout=4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            connect.assert_called_once_with('api.openai.com:443', 'api.openai.com')

    def test_foreign_domains_ip_literals_and_ports_are_denied_before_dns(self):
        for target in ('gitlab.corp:443', '127.0.0.1:443', 'api.openai.com:80',
                       'api.openai.com.evil.test:443', 'api.openai.com:443@evil.test:443'):
            with self.subTest(target=target), patch('analyzer.gateway.socket.getaddrinfo') as dns:
                with self.assertRaises(ValueError):
                    connect_provider(target, 'api.openai.com')
                dns.assert_not_called()

    def test_private_loopback_and_linklocal_dns_answers_are_denied(self):
        for address in ('127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1',
                        '169.254.169.254', '::1', 'fc00::1', '224.0.0.1'):
            with self.subTest(address=address), \
                    patch('analyzer.gateway.socket.getaddrinfo',
                          return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]), \
                    patch('analyzer.gateway.socket.socket') as connection:
                with self.assertRaises(ValueError):
                    connect_provider('api.openai.com:443', 'api.openai.com')
                connection.assert_not_called()

    def test_validated_numeric_address_is_used_without_second_dns_lookup(self):
        address = ('1.1.1.1', 443)  # Synthetic DNS answer, never connected.
        with patch('analyzer.gateway.socket.getaddrinfo', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', address)]) as dns, \
                patch('analyzer.gateway.socket.socket') as connection:
            connect_provider('api.openai.com:443', 'api.openai.com')
            connection.return_value.connect.assert_called_once_with(address)
            dns.assert_called_once()

    def test_non_connect_request_never_opens_upstream(self):
        from unittest.mock import Mock
        client = Mock()
        client.recv.side_effect = [bytes([byte]) for byte in b'GET http://api.openai.com/ HTTP/1.1\r\n\r\n']
        with patch('analyzer.gateway.connect_provider') as connect:
            with self.assertRaises(ValueError):
                handle_connect(client, 'api.openai.com')
            connect.assert_not_called()
