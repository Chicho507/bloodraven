from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from test_app import settings, login


class BrandingTests(unittest.TestCase):
    def test_branch_directory_is_private_escaped_and_independent_of_devices(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'BR_BRANDING_DIR': directory}):
            names = ['Oficina Central', 'Centro <script>alert(1)</script>', ' oficina central ']
            (Path(directory) / 'sites.json').write_text(json.dumps(names), encoding='utf-8')
            with TestClient(create_app(settings(directory), start_workers=False)) as client:
                self.assertNotIn('Oficina Central', client.get('/login').text)
                self.assertEqual(client.get('/', follow_redirects=False).status_code, 303)
                login(client)
                before = client.get('/api/inventory').json()['devices']
                for url in ['/', '/manage']:
                    content = client.get(url).text
                    self.assertIn('2 sucursales', content)
                    self.assertIn('Oficina Central', content)
                    self.assertIn('&lt;script&gt;', content)
                    self.assertNotIn('<script>alert(1)</script>', content)
                    self.assertNotIn('{{BR_BRANCH_DIRECTORY}}', content)
                self.assertIn('<option value="Oficina Central">', client.get('/manage').text)
                self.assertEqual(client.get('/static/sites.json').status_code, 404)
                self.assertEqual(before, client.get('/api/inventory').json()['devices'])

    def test_invalid_branch_configuration_does_not_break_pages(self):
        from app.branding import branch_names
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'BR_BRANDING_DIR': directory}):
            path = Path(directory) / 'sites.json'
            for data in ['{', '{}', '[null]', '[" "]', json.dumps(['A' * 81]), json.dumps(['Valid'] * 101)]:
                path.write_text(data, encoding='utf-8')
                self.assertEqual(branch_names(), [])

    def test_generic_login_and_fixed_logo_route(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
            'BR_ORGANIZATION': 'Demo Lab', 'BR_REGION': 'Testing', 'BR_BRANDING_DIR': directory,
        }):
            with TestClient(create_app(settings(directory), start_workers=False)) as client:
                response = client.get('/login')
                self.assertIn('Demo Lab', response.text)
                self.assertIn('brand-wordmark', response.text)
                self.assertNotIn('<img', response.text)
                self.assertEqual(client.get('/static/brand-logo.png').status_code, 404)
                # No arbitrary file can be exposed through the branding directory.
                (Path(directory) / 'secret.txt').write_text('private test data')
                self.assertNotIn('private test data', client.get('/static/secret.txt').text)
                png = b'\x89PNG\r\n\x1a\n'
                (Path(directory) / 'logo.png').write_bytes(png)
                logo = client.get('/static/brand-logo.png')
                self.assertEqual(logo.content, png)
                self.assertEqual(logo.headers['content-type'], 'image/png')
                self.assertIn('/static/brand-logo.png', client.get('/login').text)
                login(client)
                self.assertEqual(client.get('/static/secret.txt').status_code, 404)

    def test_brand_values_are_escaped_on_all_pages_without_recursive_expansion(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {
            'BR_ORGANIZATION': '<script>alert(1)</script>{{BR_REGION}}',
            'BR_REGION': '"Example & Lab"', 'BR_BRANDING_DIR': directory,
        }):
            with TestClient(create_app(settings(directory), start_workers=False)) as client:
                public = client.get('/login').text
                login(client)
                for content in [public, *(client.get(url).text for url in ['/', '/manage', '/account'])]:
                    self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;{{BR_REGION}}', content)
                    self.assertNotIn('<script>alert(1)</script>', content)
