"""Persistent accounts, revocable sessions, encrypted inventory and audit trail."""
import base64
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VERSION = "0.2.0-beta.1"
PROFILES = {
    "cisco_cbs350": {"vendor": "Cisco", "label": "Cisco Business CBS350", "metrics": "Disponibilidad, tráfico, CPU y temperatura; validar sensor y firmware."},
    "cisco_sg350": {"vendor": "Cisco", "label": "Cisco Small Business SG350", "metrics": "Disponibilidad y tráfico. Requiere revisión del aviso de firmware SG350."},
    "cisco_generic": {"vendor": "Cisco", "label": "Cisco · SNMP estándar", "metrics": "Disponibilidad y tráfico IF-MIB. CPU y temperatura pendientes de validar."},
    "dell_generic": {"vendor": "Dell", "label": "Dell · SNMP estándar", "metrics": "Disponibilidad y tráfico IF-MIB. Confirmar modelo, firmware y compatibilidad SNMPv3."},
    "unifi_generic": {"vendor": "UniFi", "label": "UniFi · SNMP estándar", "metrics": "Disponibilidad y tráfico IF-MIB. Habilitar SNMPv3 en UniFi Network y validar el modelo."},
}


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return "pbkdf2_sha256$600000$" + salt.hex() + "$" + digest.hex()


def password_valid(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login_name(value):
    value = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9@._+\-]{2,119}", value):
        raise ValueError("Usuario inválido: usa entre 3 y 120 caracteres")
    return value


class InventoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=2, max_length=80)
    site: str = Field(min_length=2, max_length=80)
    model: str = Field(min_length=2, max_length=100)
    profile: str
    host: str | None = Field(default=None, max_length=253)
    port: int = Field(default=161, ge=1, le=65535)
    enabled: bool = False
    interface_index: int | None = Field(default=None, ge=1, le=2147483647)
    firmware: str = Field(default="", max_length=100)
    firmware_reviewed: bool = False
    warn_temperature_c: float | None = Field(default=None, ge=0, le=150, allow_inf_nan=False)
    location: str = Field(default="", max_length=160)
    notes: str = Field(default="", max_length=1000)
    auth_protocol: str = "SHA256"
    snmp_username: str = Field(default="", max_length=32)
    auth_password: str = Field(default="", max_length=128)
    privacy_password: str = Field(default="", max_length=128)
    revision: int | None = None

    @field_validator("name", "site", "model")
    @classmethod
    def required_text(cls, value):
        value = value.strip()
        if len(value) < 2 or any(ord(c) < 32 for c in value):
            raise ValueError("Introduce un texto válido de al menos dos caracteres")
        return value

    @field_validator("profile")
    @classmethod
    def profile_valid(cls, value):
        if value not in PROFILES:
            raise ValueError("Perfil no soportado")
        return value

    @field_validator("auth_protocol")
    @classmethod
    def protocol_valid(cls, value):
        if value not in {"SHA", "SHA256"}:
            raise ValueError("Selecciona SHA o SHA256")
        return value

    @field_validator("host")
    @classmethod
    def host_valid(cls, value):
        if not value:
            return None
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if not re.fullmatch(r"(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", value):
                raise ValueError("Introduce una IPv4 o un nombre DNS, sin URL ni puerto")
            if value.casefold() == "localhost" or value.casefold().endswith(".localhost"):
                raise ValueError("La dirección debe ser la de un equipo de red")
            return value.casefold()
        if address.version != 4 or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved:
            raise ValueError("Introduce una IPv4 de gestión válida")
        return str(address)

    @model_validator(mode="after")
    def complete(self):
        if "SG350" in self.model.upper() and self.profile != "cisco_sg350":
            raise ValueError("Los SG350 requieren el perfil Cisco SG350")
        if self.enabled and not self.host:
            raise ValueError("Completa la IP o DNS antes de habilitar el monitoreo")
        if self.enabled and self.profile == "cisco_sg350" and (not self.firmware or not self.firmware_reviewed):
            raise ValueError("Revisa el firmware y el aviso SG350 antes de habilitarlo")
        supplied = (self.snmp_username, self.auth_password, self.privacy_password)
        if any(supplied) and (not all(supplied) or len(self.snmp_username.encode()) > 32 or min(len(self.auth_password.encode()), len(self.privacy_password.encode())) < 8):
            raise ValueError("Para actualizar SNMP introduce usuario y ambas claves (mínimo 8 bytes)")
        return self


class ControlError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


class Control:
    def __init__(self, settings):
        self.settings = settings
        directory = Path(settings.db_path).parent
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "control.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, role TEXT NOT NULL, password TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                csrf TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (kind TEXT NOT NULL, target TEXT NOT NULL, at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS attempts_lookup ON attempts(kind,target,at);
            CREATE TABLE IF NOT EXISTS inventory (id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                credentials TEXT, archived INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS control_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        key = os.getenv("BR_ENCRYPTION_KEY", "")
        key_file = directory / "inventory.key"
        if not key:
            if key_file.exists():
                key = key_file.read_text(encoding="ascii").strip()
            else:
                if self.db.execute("SELECT 1 FROM inventory WHERE credentials IS NOT NULL LIMIT 1").fetchone():
                    raise ValueError("Falta la clave de cifrado del inventario; restaurar la clave original")
                key = Fernet.generate_key().decode()
                fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(key)
        self.cipher = Fernet(key.encode())
        probe = self.db.execute("SELECT value FROM control_meta WHERE key='key_check'").fetchone()
        if probe:
            self.cipher.decrypt(probe[0].encode())
        else:
            self.db.execute("INSERT INTO control_meta VALUES ('key_check',?)", (self.cipher.encrypt(b"bloodraven-inventory").decode(),))
        if not self.db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            self.db.execute("INSERT INTO users VALUES (?,?,?,?,?,1)", (secrets.token_hex(12), login_name(settings.username), "Administrador", "admin", password_hash(settings.password)))
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        if not self.db.execute("SELECT 1 FROM control_meta WHERE key='inventory_seeded'").fetchone():
            for original in settings.devices:
                self.db.execute("INSERT INTO inventory(id,payload) VALUES (?,?)", (original["id"], json.dumps({**original, "demo_seed": True})))
            self.db.execute("INSERT INTO control_meta VALUES ('inventory_seeded','1')")
        self.db.commit()
        self.reload_inventory()

    def _audit(self, actor, action, target="", detail=""):
        self.db.execute("INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)", (time.time(), actor, action, target[:120], detail[:200]))

    def reload_inventory(self):
        with self.lock:
            rows = self.db.execute("SELECT * FROM inventory WHERE archived=0 ORDER BY rowid").fetchall()
            self.settings.devices = [{**json.loads(row["payload"]), "revision": row["revision"]} for row in rows]

    @staticmethod
    def public_user(row):
        return {key: row[key] for key in ("id", "username", "name", "role", "active")}

    def authenticate(self, username, password, client_ip):
        username = username.strip().casefold()
        now = time.time()
        with self.lock:
            self.db.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
            account_failures = self.db.execute("SELECT count(*) FROM attempts WHERE kind='account' AND target=?", (username,)).fetchone()[0]
            ip_failures = self.db.execute("SELECT count(*) FROM attempts WHERE kind='ip' AND target=?", (client_ip,)).fetchone()[0]
            if account_failures >= 5 or ip_failures >= 30:
                self.db.commit()
                raise ControlError("Demasiados intentos. Intenta de nuevo dentro de 15 minutos.", 429)
            row = self.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            valid = password_valid(password, row["password"] if row else self.dummy_hash)
            if not row or not row["active"] or not valid:
                self.db.executemany("INSERT INTO attempts VALUES (?,?,?)", [("account", username, now), ("ip", client_ip, now)])
                self._audit("anónimo", "login.failed", username)
                self.db.commit()
                raise ControlError("Usuario o contraseña incorrectos.", 401)
            self.db.execute("DELETE FROM attempts WHERE kind='account' AND target=?", (username,))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self.db.execute("DELETE FROM sessions WHERE created<? OR seen<?", (now - 28800, now - 1800))
            # Keep at most five sessions per account.
            self.db.execute("DELETE FROM sessions WHERE token IN (SELECT token FROM sessions WHERE user_id=? ORDER BY created DESC LIMIT -1 OFFSET 4)", (row["id"],))
            self.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (token_hash(token), row["id"], csrf, now, now))
            self._audit(row["username"], "login.success")
            self.db.commit()
            return token, csrf, self.public_user(row)

    def session(self, token, touch=True):
        if not token or len(token) > 128:
            return None
        now = time.time()
        with self.lock:
            row = self.db.execute("SELECT users.*, sessions.csrf,sessions.created,sessions.seen FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=?", (token_hash(token),)).fetchone()
            if not row or not row["active"] or now - row["created"] >= 28800 or now - row["seen"] >= 1800:
                return None
            if touch:
                self.db.execute("UPDATE sessions SET seen=? WHERE token=?", (now, token_hash(token)))
                self.db.commit()
            return {"user": self.public_user(row), "csrf": row["csrf"]}

    def logout(self, token, actor):
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE token=?", (token_hash(token),))
            self._audit(actor, "logout")
            self.db.commit()

    def inventory(self):
        with self.lock:
            output = []
            for row in self.db.execute("SELECT * FROM inventory ORDER BY rowid").fetchall():
                data = json.loads(row["payload"])
                refs = data.get("snmp", {})
                env_ready = all(os.getenv(refs.get(k, ""), "") for k in ("username_env", "auth_password_env", "privacy_password_env"))
                data.pop("snmp", None)
                data.pop("demo_seed", None)
                output.append({**data, "vendor": PROFILES[data["profile"]]["vendor"], "credentials_configured": bool(row["credentials"]) or env_ready,
                               "credential_source": "cifrado" if row["credentials"] else "servidor" if env_ready else "pendiente",
                               "revision": row["revision"], "archived": bool(row["archived"])})
            return output

    def save_device(self, body, actor, editing=False):
        data = body.model_dump()
        username, auth, privacy = (data.pop(key) for key in ("snmp_username", "auth_password", "privacy_password"))
        revision = data.pop("revision")
        with self.lock:
            old = self.db.execute("SELECT * FROM inventory WHERE id=?", (body.id,)).fetchone()
            if old and not editing:
                raise ControlError("Ya existe un equipo con ese identificador.", 409)
            if editing and not old:
                raise ControlError("Equipo desconocido.", 404)
            if old and (old["archived"] or revision != old["revision"]):
                raise ControlError("El equipo cambió o está archivado. Actualiza el inventario.", 409)
            if not old and self.db.execute("SELECT count(*) FROM inventory WHERE archived=0").fetchone()[0] >= 256:
                raise ControlError("Esta beta admite hasta 256 equipos activos.", 409)
            for row in self.db.execute("SELECT id,payload FROM inventory WHERE archived=0 AND id<>?", (body.id,)):
                if body.host and json.loads(row["payload"]).get("host") == body.host:
                    raise ControlError("La dirección ya está registrada en otro equipo.", 409)
            prior = json.loads(old["payload"]) if old else {}
            refs = prior.get("snmp", {"username_env": "SNMP_USERNAME", "auth_password_env": "SNMP_AUTH_PASSWORD", "privacy_password_env": "SNMP_PRIVACY_PASSWORD"})
            data["snmp"] = {**refs, "auth_protocol": data["auth_protocol"], "privacy_protocol": "AES128"}
            encrypted = old["credentials"] if old else None
            if username:
                encrypted = self.cipher.encrypt(json.dumps([username, auth, privacy]).encode()).decode()
            if body.enabled and not encrypted and not all(os.getenv(refs.get(k, ""), "") for k in ("username_env", "auth_password_env", "privacy_password_env")):
                raise ControlError("Configura las credenciales SNMP antes de habilitar el equipo.")
            if old:
                self.db.execute("UPDATE inventory SET payload=?,credentials=?,revision=revision+1 WHERE id=?", (json.dumps(data), encrypted, body.id))
            else:
                self.db.execute("INSERT INTO inventory(id,payload,credentials) VALUES (?,?,?)", (body.id, json.dumps(data), encrypted))
            self._audit(actor, "device.updated" if old else "device.created", body.id, "Credenciales actualizadas" if username else "Configuración guardada")
            self.db.commit()
            self.reload_inventory()

    def archive_device(self, device_id, revision, actor):
        with self.lock:
            cursor = self.db.execute("UPDATE inventory SET archived=1,revision=revision+1 WHERE id=? AND revision=? AND archived=0", (device_id, revision))
            if not cursor.rowcount:
                raise ControlError("El equipo cambió o ya está archivado. Actualiza la lista.", 409)
            self._audit(actor, "device.archived", device_id, "Historial conservado")
            self.db.commit()
            self.reload_inventory()

    def poll_config(self, device):
        with self.lock:
            row = self.db.execute("SELECT credentials FROM inventory WHERE id=? AND archived=0", (device["id"],)).fetchone()
            if not row:
                raise ControlError("Equipo archivado", 404)
            values = json.loads(self.cipher.decrypt(row[0].encode())) if row[0] else None
            return {**device, "_credentials": values} if values else dict(device)

    def users(self):
        with self.lock:
            return [self.public_user(row) for row in self.db.execute("SELECT * FROM users ORDER BY username")]

    def save_user(self, username, name, role, active, password, actor, user_id=None):
        username = login_name(username)
        if role not in {"admin", "viewer"} or not 2 <= len(name.strip()) <= 80:
            raise ControlError("Nombre o perfil inválido.")
        if password and (not 16 <= len(password) <= 128 or password.casefold() in {"passwordpassword", "1234567890123456"}):
            raise ControlError("Usa una contraseña de 16 a 128 caracteres.")
        with self.lock:
            old = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone() if user_id else None
            if user_id and not old:
                raise ControlError("Usuario desconocido.", 404)
            if not old and not password:
                raise ControlError("Introduce una contraseña inicial.")
            if old and old["username"] == actor and (not active or role != "admin"):
                raise ControlError("No puedes desactivar tu cuenta ni quitarte el acceso de administrador.")
            if old and old["active"] and old["role"] == "admin" and (not active or role != "admin"):
                if self.db.execute("SELECT count(*) FROM users WHERE active=1 AND role='admin'").fetchone()[0] <= 1:
                    raise ControlError("Debe existir al menos un administrador activo.")
            identity = user_id or secrets.token_hex(12)
            encoded = password_hash(password) if password else old["password"]
            try:
                if old:
                    self.db.execute("UPDATE users SET username=?,name=?,role=?,active=?,password=? WHERE id=?", (username, name.strip(), role, int(active), encoded, identity))
                    self.db.execute("DELETE FROM sessions WHERE user_id=?", (identity,))
                else:
                    self.db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (identity, username, name.strip(), role, encoded, int(active)))
            except sqlite3.IntegrityError:
                self.db.rollback()
                raise ControlError("Ese usuario ya existe.", 409)
            self._audit(actor, "user.updated" if old else "user.created", username, "Sesiones revocadas" if old else role)
            self.db.commit()

    def change_password(self, user, current, new):
        if not 16 <= len(new) <= 128 or hmac.compare_digest(current.encode(), new.encode()):
            raise ControlError("Usa una contraseña nueva de 16 a 128 caracteres.")
        with self.lock:
            row = self.db.execute("SELECT password FROM users WHERE id=?", (user["id"],)).fetchone()
            if not password_valid(current, row[0]):
                raise ControlError("La contraseña actual no es correcta.", 403)
            self.db.execute("UPDATE users SET password=? WHERE id=?", (password_hash(new), user["id"]))
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            self._audit(user["username"], "password.changed", user["username"], "Todas las sesiones revocadas")
            self.db.commit()

    def audit(self):
        with self.lock:
            return [dict(row) for row in self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500")]

    def close(self):
        self.db.close()
