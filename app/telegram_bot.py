"""Optional, read-only Telegram commands; importing this module starts no network IO.

The application owns enablement and secrets. ``run_bot`` polls only while enabled,
answers allowlisted users in their private chat, and never sends unsolicited alerts.
Responses are best effort: bounded retries and a persisted offset prevent repeatedly
replaying a failed command. A crash between sending and saving can duplicate a reply.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx


LOGGER = logging.getLogger(__name__)
_COMMANDS = {"/estado", "/sucursal", "/alertas", "/historial 24h"}
_LABELS = {
    "ok": "Operativo",
    "warning": "Advertencia",
    "offline": "Sin respuesta SNMP",
    "pending": "Sin primera lectura",
    "stale": "Datos desactualizados",
}
_KEYBOARD = {"inline_keyboard": [
    [{"text": "Estado", "callback_data": "/estado"},
     {"text": "Sucursales", "callback_data": "/sucursal"}],
    [{"text": "Alertas", "callback_data": "/alertas"},
     {"text": "Historial 24 h", "callback_data": "/historial 24h"}],
]}


def _plain(value: Any, maximum: int = 140) -> str:
    """Keep inventory strings on one line; no HTML or Markdown is interpreted."""
    return " ".join(str(value or "—").split())[:maximum]


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except ValueError:
        return None


def _stamp(value: Any) -> str:
    result = _date(value)
    return result.astimezone(timezone.utc).strftime("%d/%m %H:%M:%S UTC") if result else "sin lectura"


def _site(device: dict) -> str:
    site = device.get("site")
    if isinstance(site, dict):
        return _plain(site.get("name") or site.get("id") or "Sin sucursal")
    return _plain(site or "Sin sucursal")


def _site_key(value: Any) -> str:
    text = unicodedata.normalize("NFD", " ".join(str(value).split()).casefold())
    return "".join(char for char in text if unicodedata.category(char) != "Mn")


def _site_aliases(device: dict) -> set[str]:
    site = device.get("site")
    values: list[Any] = [_site(device), device.get("site_id"), device.get("site_name")]
    aliases = device.get("site_aliases", [])
    if isinstance(aliases, list):
        values.extend(aliases)
    if isinstance(site, dict):
        values.extend([site.get("id"), site.get("name")])
        if isinstance(site.get("aliases"), list):
            values.extend(site["aliases"])
    return {_site_key(value) for value in values if value}


def _metric(value: Any, suffix: str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:.1f}{suffix}"
    return "N/D"


def _device_line(device: dict, detailed: bool = False) -> str:
    state = device.get("status", "pending")
    text = f"• {_plain(device.get('name') or device.get('id'))}: {_LABELS.get(state, 'Estado desconocido')}"
    if not detailed:
        return text
    if state in {"stale", "offline", "pending"}:
        return text + f"\n  Última lectura válida: {_stamp(device.get('last_success_at'))}. Valores no actuales."
    text += f"\n  CPU {_metric(device.get('cpu_percent'), '%')} · Temperatura {_metric(device.get('temperature_c'), ' °C')}"
    interface = device.get("interface")
    interface_name = interface.get("name") or interface.get("index") if isinstance(interface, dict) else None
    text += f"\n  Interfaz {_plain(interface_name)}: entrada {_metric(device.get('rx_mbps'), ' Mbps')} · salida {_metric(device.get('tx_mbps'), ' Mbps')}"
    return text + f"\n  Lectura: {_stamp(device.get('last_success_at'))}"


def _fit_message(text: str) -> str:
    # Telegram counts UTF-16 code units. Leave room below its 4096-unit limit.
    encoded = text.encode("utf-16-le")
    if len(encoded) <= 7800:
        return text
    suffix = "\n… Resumen truncado. Consulta /sucursal nombre o el panel para más detalle."
    maximum = 3900 - len(suffix.encode("utf-16-le")) // 2
    return encoded[:maximum * 2].decode("utf-16-le", errors="ignore").rstrip() + suffix


def format_command(command: str, overview: dict, events: list[dict]) -> str:
    """Render a deterministic, plain-text reply without IO or wall-clock reads."""
    words = command.strip().split(maxsplit=1)
    name = words[0].split("@", 1)[0].casefold() if words else "/help"
    argument = " ".join(words[1].split()) if len(words) > 1 else ""
    devices = [item for item in overview.get("devices", []) if isinstance(item, dict)]
    mode = "DEMO · Datos simulados" if overview.get("mode") == "demo" else "LIVE · Datos del recolector"
    lines = [f"BloodRaven · {mode}", f"Consulta: {_stamp(overview.get('generated_at'))}"]
    lines.append(f"Último ciclo: {_stamp(overview.get('last_poll_at'))}")

    if name == "/estado":
        counts = {state: sum(d.get("status") == state for d in devices) for state in _LABELS}
        lines.extend(["", f"Estado general · {len(devices)} equipos",
                      f"Operativos {counts['ok']} · Advertencias {counts['warning']}",
                      f"Sin respuesta {counts['offline']} · Sin primera lectura {counts['pending']} · Desactualizados {counts['stale']}"])
        for site in sorted({_site(d) for d in devices}, key=str.casefold):
            lines.extend(["", site])
            lines.extend(_device_line(d) for d in devices if _site(d) == site)
        if not devices:
            lines.append("Sin equipos configurados.")
        lines.append("\nDetalle: /sucursal nombre")
    elif name == "/sucursal":
        if not argument:
            sites = sorted({_site(d) for d in devices}, key=str.casefold)
            lines.extend(["", "Sucursales disponibles:", *[f"• {site}" for site in sites]])
            if not sites:
                lines.append("Sin equipos configurados.")
            lines.append("\nUsa /sucursal nombre (ejemplo: /sucursal sede-uno).")
        else:
            matches = [d for d in devices if _site_key(argument) in _site_aliases(d)]
            lines.extend(["", f"Sucursal: {_plain(argument)}"])
            if not matches:
                lines.append("No encuentro esa sucursal. Usa /sucursal para ver los nombres disponibles.")
            lines.extend(_device_line(d, detailed=True) for d in matches)
            if matches:
                lines.append("\nEntrada/salida son tráfico de la interfaz; no una prueba de velocidad de internet.")
    elif name == "/alertas":
        issues = [d for d in devices if d.get("status") != "ok"]
        lines.extend(["", "Alertas actuales"])
        if not devices:
            lines.append("Sin equipos configurados; todavía no hay estado que evaluar.")
        elif not issues:
            lines.append("Sin alertas en la última información disponible.")
        for device in issues:
            lines.extend([_site(device), _device_line(device, detailed=True)])
        lines.append("\nSin respuesta indica que el recolector no obtuvo respuesta SNMP; no confirma que el equipo esté apagado.")
    elif name == "/historial":
        if argument.casefold() not in {"", "24h", "24 h"}:
            lines.append("\nUsa /historial 24h. Este comando consulta las últimas 24 horas.")
        else:
            now = _date(overview.get("generated_at"))
            cutoff = now - timedelta(hours=24) if now else None
            # Include incidents that overlapped the window, even if they began earlier.
            matching = []
            for event in events:
                if not isinstance(event, dict):
                    continue
                start, end = _date(event.get("started_at")), _date(event.get("ended_at"))
                if now and start and start > now:
                    continue
                if cutoff and end and end < cutoff:
                    continue
                matching.append(event)
            matching.sort(key=lambda e: _date(e.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            lines.extend(["", f"Historial de las últimas 24 h · {len(matching)} eventos"])
            if not matching:
                lines.append("No hay eventos registrados en ese período.")
            for event in matching[:25]:
                state = "Abierto" if not event.get("ended_at") else f"Cerrado {_stamp(event['ended_at'])}"
                lines.append(f"• {_plain(event.get('site'))} / {_plain(event.get('device_name') or event.get('device_id'))} · {_plain(event.get('kind'))}")
                lines.append(f"  Desde {_stamp(event.get('started_at'))} · {state}")
                lines.append(f"  {_plain(event.get('message'), 220)}")
            if len(matching) > 25:
                lines.append("Se muestran los 25 más recientes. Consulta el panel para ver el resto.")
    else:
        lines.extend(["", "Consultas disponibles:", "/estado", "/sucursal nombre", "/alertas", "/historial 24h",
                      "Usa también los botones. Las consultas no cambian equipos ni configuración."])
    return _fit_message("\n".join(lines))


def _authorized_request(update: dict, allowed_user_ids: set[int]) -> tuple[int, str, str | None] | None:
    """Extract an authorized command, applying the same rules to button callbacks."""
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from")
        message = callback.get("message")
        command = callback.get("data")
        callback_id = callback.get("id")
        if not isinstance(command, str) or command not in _COMMANDS or not isinstance(callback_id, str):
            return None
    else:
        message = update.get("message")
        sender = message.get("from") if isinstance(message, dict) else None
        command = message.get("text") if isinstance(message, dict) else None
        callback_id = None
    if not isinstance(message, dict) or not isinstance(sender, dict) or not isinstance(command, str):
        return None
    user_id = sender.get("id")
    chat = message.get("chat")
    if (type(user_id) is not int or user_id not in allowed_user_ids or sender.get("is_bot")
            or not isinstance(chat, dict) or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat["id"] != user_id):
        return None
    return user_id, command[:512], callback_id


class _Stopped(Exception):
    pass


class _ApiFailure(Exception):
    """Only sanitized status information; never retain URLs, response text or tokens."""

    def __init__(self, code: int = 0, retry_after: float | None = None):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API status {code}")


class _SecretLogFilter(logging.Filter):
    """HTTPX normally logs request URLs at INFO, including the Bot API token."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        return self._token not in record.getMessage()


async def _pause(stop_event: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(0.01, delay))
        return True
    except TimeoutError:
        return False


async def _api_call(client: httpx.AsyncClient, url: str, payload: dict, stop_event: asyncio.Event) -> Any:
    if stop_event.is_set():
        raise _Stopped
    request = asyncio.create_task(client.post(url, json=payload))
    stopped = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({request, stopped}, return_when=asyncio.FIRST_COMPLETED)
        if stop_event.is_set():
            raise _Stopped
        try:
            response = await request
        except httpx.HTTPError:
            raise _ApiFailure() from None
        try:
            data = response.json()
        except ValueError:
            raise _ApiFailure(response.status_code) from None
        if not isinstance(data, dict):
            raise _ApiFailure(response.status_code)
        if response.is_success and data.get("ok") is True:
            return data.get("result")
        code = data.get("error_code", response.status_code)
        code = code if type(code) is int else response.status_code
        parameters = data.get("parameters")
        retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
        retry = float(retry) if isinstance(retry, (int, float)) and not isinstance(retry, bool) and math.isfinite(retry) else None
        raise _ApiFailure(code, max(1.0, retry) if retry is not None else None)
    finally:
        for task in (request, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(request, stopped, return_exceptions=True)


async def _reply_call(client: httpx.AsyncClient, url: str, payload: dict, stop_event: asyncio.Event) -> Any:
    for attempt in range(3):
        try:
            return await _api_call(client, url, payload, stop_event)
        except _ApiFailure as error:
            # Invalid, blocked or expired destinations should not be retried.
            if 400 <= error.code < 500 and error.code != 429:
                raise
            delay = error.retry_after if error.code == 429 and error.retry_after is not None else min(2 ** attempt, 30)
            if await _pause(stop_event, delay):
                raise _Stopped from None
            if attempt == 2:
                raise


def _offset_key(token: str) -> str:
    # A replacement bot must not reuse a different bot's offset; never store secrets.
    return "telegram_offset_" + hashlib.sha256(token.encode()).hexdigest()[:24]


async def run_bot(store: Any, stop_event: asyncio.Event, token: str, allowed_user_ids: set[int]) -> None:
    """Run long polling until stopped. Empty credentials/allowlist mean disabled.

    The caller must start at most one worker per bot token. No webhook is created
    or deleted. Every processed or ignored update advances the persisted offset.
    """
    if not token or not allowed_user_ids or stop_event.is_set():
        return
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        LOGGER.warning("Telegram desactivado: formato de token no válido.")
        return
    allowed_user_ids = {uid for uid in allowed_user_ids if type(uid) is int and uid > 0}
    if not allowed_user_ids:
        return
    key = _offset_key(token)
    try:
        offset = max(0, int(store.get_meta(key) or 0))
    except (TypeError, ValueError):
        offset = 0
    except Exception:
        LOGGER.error("Telegram detenido: no se pudo leer el marcador persistente.")
        return
    base_url = f"https://api.telegram.org/bot{token}/"
    log_filter = _SecretLogFilter(token)
    filtered_loggers = [logging.getLogger(name) for name in (
        "httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy")]
    for logger in filtered_loggers:
        logger.addFilter(log_filter)
    backoff = 1.0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0), follow_redirects=False) as client:
            while not stop_event.is_set():
                try:
                    updates = await _api_call(client, base_url + "getUpdates", {
                        "offset": offset, "timeout": 30, "limit": 50,
                        "allowed_updates": ["message", "callback_query"],
                    }, stop_event)
                    if not isinstance(updates, list):
                        raise _ApiFailure()
                    backoff = 1.0
                except _ApiFailure as error:
                    if error.code in {401, 403}:
                        LOGGER.error("Telegram detenido: credenciales rechazadas (código %s).", error.code)
                        return
                    LOGGER.warning("Telegram: consulta no disponible (código %s); se reintentará.", error.code)
                    delay = error.retry_after if error.code == 429 and error.retry_after is not None else backoff
                    if await _pause(stop_event, delay):
                        return
                    backoff = min(backoff * 2, 30.0)
                    continue
                for update in updates:
                    if stop_event.is_set():
                        return
                    if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                        continue
                    update_id = update["update_id"]
                    if update_id < offset:
                        continue
                    request = _authorized_request(update, allowed_user_ids)
                    if request is not None:
                        user_id, command, callback_id = request
                        try:
                            if callback_id is not None:
                                try:
                                    await _reply_call(client, base_url + "answerCallbackQuery", {"callback_query_id": callback_id}, stop_event)
                                except _ApiFailure:
                                    LOGGER.warning("Telegram: no se pudo confirmar un botón.")
                            overview = store.snapshot()
                            events = store.events(hours=24) if command.strip().split("@", 1)[0].casefold().startswith("/historial") else []
                            text = format_command(command, overview, events)
                            await _reply_call(client, base_url + "sendMessage", {
                                "chat_id": user_id, "text": text,
                                "reply_markup": _KEYBOARD,
                                "link_preview_options": {"is_disabled": True},
                            }, stop_event)
                        except _ApiFailure as error:
                            LOGGER.warning("Telegram: respuesta no entregada (código %s); consulta procesada.", error.code)
                        except _Stopped:
                            raise
                        except Exception:
                            LOGGER.error("Telegram: no se pudo preparar la respuesta; consulta procesada.")
                    offset = update_id + 1
                    try:
                        store.set_meta(key, str(offset))
                    except Exception:
                        LOGGER.error("Telegram detenido: no se pudo guardar el marcador persistente.")
                        return
    except _Stopped:
        return
    finally:
        for logger in filtered_loggers:
            logger.removeFilter(log_filter)
