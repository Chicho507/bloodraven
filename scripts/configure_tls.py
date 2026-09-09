"""Configure optional DNS-01 TLS locally; never print or upload credentials."""
import argparse
from datetime import datetime, timezone
import getpass
import ipaddress
import os
from pathlib import Path
import re
import tempfile


def validate_address(hostname, bind_ip):
    labels = hostname.split('.')
    if (len(hostname) > 253 or len(labels) < 2
            or not re.fullmatch(r'[a-zA-Z]{2,63}', labels[-1])
            or any(not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label) for label in labels)):
        raise ValueError('Usa un nombre DNS completo, sin https://, puerto ni comodines.')
    address = ipaddress.IPv4Address(bind_ip)
    if not any(address in ipaddress.ip_network(network) for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
        raise ValueError('La IP de escucha debe ser una IPv4 interna RFC1918 del servidor, no la IP de NAT.')


def write_private(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
        Path(temporary).chmod(0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure(root, hostname, bind_ip, token=None):
    validate_address(hostname, bind_ip)
    environment = root / '.env'
    if not environment.is_file():
        raise ValueError('Primero ejecuta python3 scripts/configure.py para preparar .env.')
    original = environment.read_text(encoding='utf-8')
    secret_dir = root / 'secrets'
    secret_path = secret_dir / 'cloudflare_api_token'
    if token is None:
        if not secret_path.is_file() or not secret_path.read_text(encoding='utf-8').strip():
            raise ValueError('Falta un token DNS local.')
    elif not re.fullmatch(r'[A-Za-z0-9_-]{20,256}', token):
        raise ValueError('Introduce únicamente el token API, sin espacios ni encabezados.')
    updates = {'BR_HOSTNAME': hostname.lower(), 'BR_PUBLIC_ORIGIN': 'https://' + hostname.lower(),
               'BR_COOKIE_SECURE': 'true', 'BR_BIND_IP': bind_ip,
               'COMPOSE_FILE': 'compose.yaml:compose.cloudflare.yaml'}
    pattern = re.compile(r'^\s*(?:export\s+)?(' + '|'.join(updates) + r')\s*=')
    lines = [line for line in original.splitlines() if not pattern.match(line)]
    updated = '\n'.join(lines + [f'{key}={value}' for key, value in updates.items()]) + '\n'
    backup = root / ('.env.backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
    write_private(backup, original)
    secret_dir.mkdir(mode=0o700, exist_ok=True)
    secret_dir.chmod(0o700)
    if token is not None:
        write_private(secret_path, token + '\n')
    secret_path.chmod(0o600)
    write_private(environment, updated)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostname', required=True)
    parser.add_argument('--bind-ip', required=True)
    parser.add_argument('--replace-token', action='store_true', help='Guardar un token nuevo mediante entrada oculta.')
    args = parser.parse_args()
    if os.name != 'posix':
        raise SystemExit('Ejecuta este asistente en el servidor Linux.')
    root = Path(__file__).resolve().parents[1]
    try:
        validate_address(args.hostname, args.bind_ip)
        if not (root / '.env').is_file():
            raise ValueError('Primero prepara .env con scripts/configure.py.')
        token_path = root / 'secrets' / 'cloudflare_api_token'
        token = None
        if args.replace_token or not token_path.is_file() or not token_path.read_text().strip():
            if not os.isatty(0):
                raise ValueError('Introduce el token desde una terminal interactiva del servidor.')
            token = getpass.getpass('Token API de Cloudflare (entrada oculta): ').strip()
        backup = configure(root, args.hostname, args.bind_ip, token)
    except (ValueError, OSError):
        raise SystemExit('No se pudo preparar TLS. Revisa el nombre DNS, la IPv4 interna, .env, los permisos y el token local.')
    print('Configuración TLS guardada; token oculto y exclusivo del proxy.')
    print('Respaldo local de configuración: ' + backup.name)
    print('Todavía no se solicitó ningún certificado ni se cambió el DNS.')
    print('Comprueba el DNS interno/VPN y el firewall antes de reconstruir app y proxy.')


if __name__ == '__main__':
    main()
