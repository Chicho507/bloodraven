"""Bounded, read-only SNMPv3 polling for the explicitly configured switches.

Only GET is used: never WALK, GETNEXT, GETBULK, SET, discovery, or PoE tables.
The CBS350 vendor objects need validation on the installed firmware. SG350
vendor metrics deliberately stay unavailable until separately verified.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any

from pysnmp.hlapi.v3arch.asyncio import (
    USM_AUTH_HMAC192_SHA256,
    USM_AUTH_HMAC96_SHA,
    USM_PRIV_CFB128_AES,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
)
from pysnmp.proto.rfc1905 import EndOfMibView, NoSuchInstance, NoSuchObject


SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
CBS_CPU_ONE_MINUTE = "1.3.6.1.4.1.9.6.1.101.1.8.0"
CBS_TEMPERATURE = "1.3.6.1.4.1.9.6.1.101.53.15.1.10.1"
IF_HC_IN_OCTETS = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS = "1.3.6.1.2.1.31.1.1.1.10"
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_COUNTER_DISCONTINUITY = "1.3.6.1.2.1.31.1.1.1.19"

TRANSPORT_TIMEOUT_SECONDS = 2.0
TRANSPORT_RETRIES = 1
POLL_TIMEOUT_SECONDS = 12.0

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_INTEGER = re.compile(r"-?[0-9]+\Z")
_MISSING = (NoSuchInstance, NoSuchObject, EndOfMibView)


def _empty_result() -> dict[str, Any]:
    return {
        "ok": False,
        "cpu_percent": None,
        "temperature_c": None,
        "uptime_seconds": None,
        "in_octets": None,
        "out_octets": None,
        "interface_name": None,
        "interface_index": None,
        "counter_discontinuity": None,
        "error": None,
    }


def _integer(value: Any, minimum: int, maximum: int) -> int | None:
    """Parse integer counters without float rounding or coercing missing to 0."""
    if value is None or isinstance(value, (bool, *_MISSING)):
        return None
    try:
        text = str(value)
        if not _INTEGER.fullmatch(text):
            return None
        number = int(text)
        return number if minimum <= number <= maximum else None
    except (TypeError, ValueError):
        return None


def _interface_name(value: Any) -> str | None:
    if value is None or isinstance(value, _MISSING):
        return None
    try:
        raw = value.asOctets() if hasattr(value, "asOctets") else str(value).encode()
        text = raw.decode("utf-8", errors="replace")
        # Remove terminal/control characters before names reach the UI or bot.
        return "".join(char for char in text if char.isprintable())[:128] or None
    except (TypeError, ValueError):
        return None


def _credentials(config: dict[str, Any], supplied=None) -> UsmUserData:
    """Credentials originate in environment references or the encrypted vault."""
    fields = ("username_env", "auth_password_env", "privacy_password_env")
    values = []
    for field in fields if supplied is None else ():
        name = config.get(field)
        if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
            raise ValueError("invalid credential reference")
        value = os.environ.get(name)
        if not value:
            raise ValueError("missing credential")
        values.append(value)
    if supplied is not None:
        if not isinstance(supplied, list) or len(supplied) != 3 or not all(isinstance(v, str) and v for v in supplied):
            raise ValueError("invalid stored credentials")
        values = supplied
    username, auth_password, privacy_password = values
    if not 1 <= len(username.encode("utf-8")) <= 32:
        raise ValueError("invalid username")
    if min(len(auth_password.encode("utf-8")), len(privacy_password.encode("utf-8"))) < 8:
        raise ValueError("invalid passphrase")
    protocols = {"SHA256": USM_AUTH_HMAC192_SHA256, "SHA": USM_AUTH_HMAC96_SHA}
    auth_protocol = config.get("auth_protocol", "SHA256")
    if auth_protocol not in protocols or config.get("privacy_protocol", "AES128") != "AES128":
        raise ValueError("unsupported protocol")
    return UsmUserData(
        username,
        authKey=auth_password,
        privKey=privacy_password,
        authProtocol=protocols[auth_protocol],
        privProtocol=USM_PRIV_CFB128_AES,
    )


async def _get(
    engine: SnmpEngine,
    credentials: UsmUserData,
    target: UdpTransportTarget,
    oids: list[str],
) -> tuple[dict[str, Any], str | None]:
    indication, status, _, bindings = await get_cmd(
        engine,
        credentials,
        target,
        ContextData(),
        *(ObjectType(ObjectIdentity(oid)) for oid in oids),
        lookupMib=False,
    )
    # Engine/PDU exceptions may include credentials. Never return their text.
    if indication:
        return {}, "No se recibió una respuesta SNMP válida; revisar acceso y credenciales."
    if status:
        return {}, "El agente SNMP rechazó la consulta de los objetos solicitados."
    allowed = set(oids)
    return {
        str(oid).lstrip("."): value
        for oid, value in bindings
        if str(oid).lstrip(".") in allowed
    }, None


async def poll_device(device: dict[str, Any]) -> dict[str, Any]:
    """Return one snapshot; missing metrics are None and no rates are derived.

    A valid sysUpTime response is the sole reachability criterion. Optional
    metrics failing later do not turn that successful response into downtime.
    A device-level timeout bounds DNS, retries, and both GET requests together.
    """
    result = _empty_result()
    profile = device.get("profile")
    if profile not in {"cisco_cbs350", "cisco_sg350", "cisco_generic", "dell_generic", "unifi_generic"}:
        result["error"] = "Perfil SNMP sin configurar o no compatible."
        return result
    if profile == "cisco_sg350" and (
        device.get("firmware_reviewed") is not True or not device.get("firmware")
    ):
        result["error"] = "SNMP pausado: revisar firmware y aviso Cisco del SG350 antes de habilitar."
        return result

    engine = None
    try:
        host = device.get("host")
        port = device.get("port", 161)
        index = device.get("interface_index")
        if not isinstance(host, str) or not host.strip():
            raise ValueError("missing host")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if index is not None and (type(index) is not int or not 1 <= index <= 2147483647):
            raise ValueError("invalid interface index")
        snmp_config = device.get("snmp")
        if not isinstance(snmp_config, dict):
            raise ValueError("missing credentials")
        credentials = _credentials(snmp_config, device.get("_credentials"))
        result["interface_index"] = index
    except Exception:
        result["error"] = "Configuración SNMPv3 incompleta o inválida; revisar host, índice y variables de entorno."
        return result

    try:
        async with asyncio.timeout(POLL_TIMEOUT_SECONDS):
            engine = SnmpEngine()
            target = await UdpTransportTarget.create(
                (host.strip(), port),
                timeout=TRANSPORT_TIMEOUT_SECONDS,
                retries=TRANSPORT_RETRIES,
            )
            values, error = await _get(engine, credentials, target, [SYS_UPTIME])
            if error:
                result["error"] = error
                return result
            uptime = _integer(values.get(SYS_UPTIME), 0, 2**32 - 1)
            if uptime is None:
                result["error"] = "El agente respondió sin un sysUpTime válido; revisar la vista SNMP."
                return result
            result["ok"] = True
            result["uptime_seconds"] = uptime / 100

            oids = []
            if profile == "cisco_cbs350":
                oids.extend([CBS_CPU_ONE_MINUTE, CBS_TEMPERATURE])
            if index is not None:
                oids.extend(
                    f"{oid}.{index}"
                    for oid in (IF_HC_IN_OCTETS, IF_HC_OUT_OCTETS, IF_NAME, IF_COUNTER_DISCONTINUITY)
                )
            if not oids:
                return result
            values, error = await _get(engine, credentials, target, oids)
            if error:
                result["error"] = "El equipo respondió; algunas métricas no se pudieron consultar."
                return result
            if profile == "cisco_cbs350":
                cpu = _integer(values.get(CBS_CPU_ONE_MINUTE), 0, 100)
                temperature = _integer(values.get(CBS_TEMPERATURE), -273, 1000)
                result["cpu_percent"] = float(cpu) if cpu is not None else None
                result["temperature_c"] = float(temperature) if temperature is not None else None
            if index is not None:
                result["in_octets"] = _integer(values.get(f"{IF_HC_IN_OCTETS}.{index}"), 0, 2**64 - 1)
                result["out_octets"] = _integer(values.get(f"{IF_HC_OUT_OCTETS}.{index}"), 0, 2**64 - 1)
                result["interface_name"] = _interface_name(values.get(f"{IF_NAME}.{index}"))
                # Keep raw TimeTicks for equality comparisons across samples.
                result["counter_discontinuity"] = _integer(
                    values.get(f"{IF_COUNTER_DISCONTINUITY}.{index}"), 0, 2**32 - 1,
                )
            return result
    except TimeoutError:
        result["error"] = (
            "El equipo respondió; se agotó el tiempo para consultar métricas adicionales."
            if result["ok"]
            else "Se agotó el tiempo de espera de la consulta SNMP."
        )
    except Exception:
        result["error"] = (
            "El equipo respondió; falló la lectura de métricas adicionales."
            if result["ok"]
            else "No se pudo completar la consulta SNMP; revisar configuración y conectividad."
        )
    finally:
        if engine is not None:
            try:
                engine.close_dispatcher()
            except Exception:
                # Do not allow library cleanup errors to expose secret-bearing
                # exception text through an application-level error handler.
                result["error"] = "La consulta SNMP terminó con un error al liberar recursos."
    return result
