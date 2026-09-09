import os
from pathlib import Path
import tempfile
import unittest

from scripts.configure_tls import configure, validate_address


class TlsSetupTests(unittest.TestCase):
    def test_preserves_existing_settings_and_keeps_token_out_of_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = 'BR_PASSWORD=existing-private-test-password\nBR_ENCRYPTION_KEY=keep-this-test-key\nBR_MODE=live\nBR_HOSTNAME=old.example.com\nexport BR_HOSTNAME=duplicate.example.com\n'
            (root / '.env').write_text(original)
            token = 'fake-cloudflare-test-token-123456789'
            backup = configure(root, 'Monitor.Example.COM', '10.10.10.10', token)
            result = (root / '.env').read_text()
            self.assertEqual(backup.read_text(), original)
            for value in ['BR_PASSWORD=existing-private-test-password', 'BR_ENCRYPTION_KEY=keep-this-test-key', 'BR_MODE=live']:
                self.assertIn(value, result)
            self.assertEqual(result.count('BR_HOSTNAME='), 1)
            self.assertIn('BR_HOSTNAME=monitor.example.com', result)
            self.assertIn('COMPOSE_FILE=compose.yaml:compose.cloudflare.yaml', result)
            self.assertNotIn(token, result)
            self.assertEqual((root / 'secrets/cloudflare_api_token').read_text().strip(), token)
            configure(root, 'monitor.example.com', '10.10.10.10')
            self.assertEqual((root / '.env').read_text(), result)
            if os.name == 'posix':
                for path in [root / '.env', backup, root / 'secrets/cloudflare_api_token']:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual((root / 'secrets').stat().st_mode & 0o777, 0o700)

    def test_invalid_inputs_do_not_change_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.env').write_text('BR_PASSWORD=keep-test-value\n')
            for host, bind, token in [('https://monitor.example.com', '10.0.0.1', 'x' * 40),
                                      ('monitor.example.com', '0.0.0.0', 'x' * 40),
                                      ('monitor.example.com', '8.8.8.8', 'x' * 40),
                                      ('monitor.example.com', '10.0.0.1', 'bad\ntoken'),
                                      ('monitor.example.com', '10.0.0.1', None)]:
                with self.subTest(host=host, bind=bind), self.assertRaises(ValueError):
                    configure(root, host, bind, token)
            self.assertEqual((root / '.env').read_text(), 'BR_PASSWORD=keep-test-value\n')
            self.assertFalse((root / 'secrets').exists())

    def test_rejects_ip_hostname_wildcards_and_configuration_injection(self):
        for hostname in ['127.0.0.1', '*.example.com', 'monitor.example.com:443', 'monitor.example.com\nadmin off', '-bad.example.com']:
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                validate_address(hostname, '10.0.0.1')
