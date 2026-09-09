"""CI-only checks of the resolved optional TLS profile; never print its config."""
import json
import subprocess


def main():
    config = json.loads(subprocess.check_output([
        'docker', 'compose', '-f', 'compose.yaml', '-f', 'compose.cloudflare.yaml', 'config', '--format', 'json',
    ], text=True))
    proxy = config['services']['proxy']
    app = config['services']['app']
    assert len(proxy['ports']) == 1, 'The wildcard host port must be replaced, not retained'
    assert proxy['ports'][0]['host_ip'] == '127.0.0.1'
    assert str(proxy['ports'][0]['published']) == '443'
    assert proxy['ports'][0]['target'] == 443
    assert proxy['ports'][0]['protocol'] == 'tcp'
    assert not app.get('ports'), 'Backend must remain unpublished'
    assert not app.get('secrets'), 'DNS credential must not be mounted in the application'
    assert proxy['secrets'][0]['source'] == 'cloudflare_api_token'
    assert 'CF_API_TOKEN' not in proxy.get('environment', {})
    assert 'CF_API_TOKEN' not in app.get('environment', {})
    assert app['environment']['BR_COOKIE_SECURE'] == 'true'
    assert app['environment']['BR_PUBLIC_ORIGIN'] == 'https://monitor.example.com'
    print('TLS profile: one scoped HTTPS port, private backend and proxy-only secret verified.')


if __name__ == '__main__':
    main()
