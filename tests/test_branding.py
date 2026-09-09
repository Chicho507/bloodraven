from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from test_app import settings, login


class BrandingTests(unittest.TestCase):
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
