from dataclasses import dataclass
from pathlib import Path
import os
import re

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SnmpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username_env: str = "SNMP_USERNAME"
    auth_password_env: str = "SNMP_AUTH_PASSWORD"
    privacy_password_env: str = "SNMP_PRIVACY_PASSWORD"
    auth_protocol: str = "SHA256"
    privacy_protocol: str = "AES128"


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    name: str
    site: str
    model: str
    profile: str
    enabled: bool = False
    host: str | None = None
    port: int = Field(default=161, ge=1, le=65535)
    interface_index: int | None = Field(default=None, ge=1)
    firmware: str = ""
    firmware_reviewed: bool = False
    warn_temperature_c: float | None = None
    snmp: SnmpConfig = Field(default_factory=SnmpConfig)


@dataclass
class Settings:
    mode: str
    username: str
    password: str
    db_path: str
    devices: list[dict]
    interval: int = 30
    stale_after: int = 95
    retention_days: int = 7
    telegram_enabled: bool = False
    telegram_token: str = ""
    telegram_allowed_ids: set[int] | None = None
    secure_cookies: bool = True
    public_origin: str = "https://localhost"


def load_settings() -> Settings:
    mode = os.getenv("BR_MODE", "demo").lower()
    if mode not in {"demo", "live"}:
        raise ValueError("BR_MODE debe ser demo o live")
    username = os.getenv("BR_USERNAME", "admin")
    password = os.getenv("BR_PASSWORD", "")
    if not username or ":" in username or len(password) < 16 or password.startswith("REPLACE_WITH_"):
        raise ValueError("Configura BR_USERNAME y BR_PASSWORD (mínimo 16 caracteres)")
    path = Path(os.getenv("BR_INVENTORY", "config/devices.yml"))
    if not path.exists() and mode == "demo":
        path = Path("config/devices.example.yml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("devices"), list):
        raise ValueError("El inventario debe contener una lista devices")
    devices = [Device.model_validate(d).model_dump() for d in raw["devices"]]
    if not devices or len({d["id"] for d in devices}) != len(devices):
        raise ValueError("El inventario necesita equipos con identificadores únicos")
    for device in devices:
        if device["profile"] not in {"cisco_cbs350", "cisco_sg350", "cisco_generic", "dell_generic", "unifi_generic"}:
            raise ValueError("Perfil SNMP no soportado")
        for key in ("username_env", "auth_password_env", "privacy_password_env"):
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", device["snmp"][key]):
                raise ValueError("Usa nombres de variables de entorno válidos para SNMP")
        if mode == "live" and device["enabled"] and not device["host"]:
            raise ValueError("Un equipo habilitado necesita host")
    interval = int(os.getenv("BR_POLL_SECONDS", "30"))
    if not 15 <= interval <= 3600:
        raise ValueError("BR_POLL_SECONDS debe estar entre 15 y 3600")
    days = int(os.getenv("BR_RETENTION_DAYS", "7"))
    if not 1 <= days <= 90:
        raise ValueError("BR_RETENTION_DAYS debe estar entre 1 y 90")
    enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ids = {int(v.strip()) for v in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if v.strip()}
    if enabled and (not token or not ids or any(i <= 0 for i in ids)):
        raise ValueError("Telegram necesita token y una lista de IDs de usuarios autorizados")
    directory = Path(os.getenv("BR_DATA_DIR", "data"))
    secure = os.getenv("BR_COOKIE_SECURE", "true").lower() == "true"
    origin = os.getenv("BR_PUBLIC_ORIGIN", "https://" + os.getenv("BR_HOSTNAME", "localhost")).rstrip("/")
    if secure and not origin.startswith("https://"):
        raise ValueError("El portal requiere HTTPS cuando BR_COOKIE_SECURE=true")
    return Settings(mode, username, password, str(directory / f"{mode}.sqlite3"), devices,
                    interval, interval * 3 + 5, days, enabled, token, ids, secure, origin)
