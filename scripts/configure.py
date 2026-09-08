"""Preparar configuración local sin sobrescribir secretos existentes."""
from pathlib import Path
import os
import re
import secrets
import shutil


def main():
    root = Path(__file__).resolve().parents[1]
    destination = root / ".env"
    if destination.exists():
        print(".env ya existe; se conserva. Edita ese archivo para cambiar opciones.")
    else:
        hostname = input("IPv4 o nombre DNS del Ubuntu (sin https://) [localhost]: ").strip() or "localhost"
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", hostname):
            raise SystemExit("Introduce únicamente la IP o un nombre DNS válido")
        password = secrets.token_urlsafe(24)
        content = (root / ".env.example").read_text(encoding="utf-8")
        content = content.replace("REPLACE_WITH_A_RANDOM_PASSWORD_BEFORE_STARTING", password)
        content = content.replace("BR_HOSTNAME=localhost", "BR_HOSTNAME=" + hostname)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        print("Creado .env con una contraseña aleatoria. Usuario: admin.")
        print("Consulta BR_PASSWORD dentro de .env en este servidor para iniciar sesión.")
    inventory = root / "config" / "devices.yml"
    if not inventory.exists():
        shutil.copyfile(root / "config" / "devices.example.yml", inventory)
        # No passwords in inventory; the unprivileged container must read it.
        inventory.chmod(0o644)
    print("Configuración lista en modo demo. Ejecuta: docker compose up -d --build")


if __name__ == "__main__":
    main()
