"""SQLite local, estados de lectura y tasas calculadas entre muestras válidas."""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utcnow()).isoformat(timespec="seconds")


def parse_stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rates(previous, result, now):
    """No convertir ausencia de datos, reinicios o discontinuidades en tráfico."""
    if not previous or result.get("in_octets") is None or result.get("out_octets") is None:
        return None, None
    if previous.get("interface_index") != result.get("interface_index"):
        return None, None
    if previous.get("uptime_seconds") is not None and result.get("uptime_seconds") is not None:
        if result["uptime_seconds"] < previous["uptime_seconds"]:
            return None, None
    if previous.get("counter_discontinuity") != result.get("counter_discontinuity"):
        return None, None
    seconds = (now - parse_stamp(previous["timestamp"])).total_seconds()
    if seconds <= 0:
        return None, None
    output = []
    for key in ("in_octets", "out_octets"):
        old, new = previous.get(key), result.get(key)
        if old is None or new is None or new < old:
            output.append(None)
        else:
            output.append(round((new - old) * 8 / seconds / 1_000_000, 3))
    return tuple(output)


class Store:
    def __init__(self, settings):
        self.settings = settings
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS current (device_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (device_id TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_device_time ON samples(device_id,timestamp);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, device_name TEXT NOT NULL,
                site TEXT NOT NULL, kind TEXT NOT NULL, started_at TEXT NOT NULL,
                ended_at TEXT, message TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON events(started_at);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        for device in settings.devices:
            old = self._current(device["id"]) or self._blank(device)
            old.update({k: device[k] for k in ("id", "name", "site", "model")})
            # A disabled live device must not inherit a healthy status from a past run.
            if settings.mode == "live" and not device["enabled"]:
                old = self._blank(device)
            self._save_current(old)
        self.db.commit()

    @staticmethod
    def _blank(device):
        return {**{k: device[k] for k in ("id", "name", "site", "model")},
                "status": "pending", "last_success_at": None, "last_attempt_at": None,
                "cpu_percent": None, "temperature_c": None, "rx_mbps": None, "tx_mbps": None,
                "uptime_seconds": None, "interface": None, "error": "Pendiente de configuración",
                "_failures": 0, "_counter": None}

    def _current(self, device_id):
        row = self.db.execute("SELECT payload FROM current WHERE device_id=?", (device_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save_current(self, value):
        self.db.execute("INSERT OR REPLACE INTO current VALUES (?,?)", (value["id"], json.dumps(value)))

    def _event_transition(self, old, value, now):
        old_status = old["status"]
        status = value["status"]
        if old_status == status:
            return
        if status in {"ok", "warning"}:
            self.db.execute("UPDATE events SET ended_at=? WHERE device_id=? AND ended_at IS NULL AND kind='offline'",
                            (stamp(now), value["id"]))
        if status == "ok":
            self.db.execute("UPDATE events SET ended_at=? WHERE device_id=? AND ended_at IS NULL AND kind='temperature'",
                            (stamp(now), value["id"]))
        if status in {"offline", "warning"}:
            kind = "offline" if status == "offline" else "temperature"
            opened = self.db.execute("SELECT id FROM events WHERE device_id=? AND kind=? AND ended_at IS NULL",
                                     (value["id"], kind)).fetchone()
            if not opened:
                message = ("Sin respuesta SNMP tras 3 consultas consecutivas fallidas" if status == "offline"
                           else f"Temperatura sobre el umbral configurado: {value['temperature_c']} °C")
                self.db.execute("INSERT INTO events(device_id,device_name,site,kind,started_at,message) VALUES (?,?,?,?,?,?)",
                                (value["id"], value["name"], value["site"], kind, stamp(now), message))

    def record(self, device, result, now=None):
        now = now or utcnow()
        with self.lock:
            old = self._current(device["id"]) or self._blank(device)
            value = dict(old)
            value["last_attempt_at"] = stamp(now)
            if result["ok"]:
                rx, tx = rates(old.get("_counter"), result, now)
                value.update({"status": "ok", "_failures": 0, "error": result.get("error"),
                              "last_success_at": stamp(now), "rx_mbps": rx, "tx_mbps": tx,
                              "cpu_percent": result.get("cpu_percent"), "temperature_c": result.get("temperature_c"),
                              "uptime_seconds": result.get("uptime_seconds"),
                              "interface": {"name": result.get("interface_name"), "index": result.get("interface_index")}
                              if result.get("interface_index") else None,
                              "_counter": {**result, "timestamp": stamp(now)}})
                threshold = device.get("warn_temperature_c")
                if threshold is not None and value["temperature_c"] is not None and value["temperature_c"] >= threshold:
                    value["status"] = "warning"
            else:
                value["_failures"] = old.get("_failures", 0) + 1
                value.update({"status": "offline" if value["_failures"] >= 3 else "stale",
                              "error": result.get("error") or "Sin respuesta SNMP", "_counter": None})
                for key in ("cpu_percent", "temperature_c", "rx_mbps", "tx_mbps", "uptime_seconds"):
                    value[key] = None
            self._event_transition(old, value, now)
            self._save_current(value)
            self._sample(value, now)
            self.db.commit()

    def _sample(self, value, now):
        payload = {k: value[k] for k in ("cpu_percent", "temperature_c", "rx_mbps", "tx_mbps", "status")}
        payload["timestamp"] = stamp(now)
        self.db.execute("INSERT INTO samples VALUES (?,?,?)", (value["id"], stamp(now), json.dumps(payload)))

    def pending(self, device, message):
        with self.lock:
            old = self._current(device["id"])
            value = self._blank(device)
            value["error"] = message
            value["last_success_at"] = old.get("last_success_at") if old else None
            self._save_current(value)
            self.db.commit()

    def snapshot(self):
        with self.lock:
            now, devices = utcnow(), []
            for device in self.settings.devices:
                value = self._current(device["id"]) or self._blank(device)
                value = {k: v for k, v in value.items() if not k.startswith("_")}
                if value["status"] in {"ok", "warning"} and value["last_success_at"]:
                    if (now - parse_stamp(value["last_success_at"])).total_seconds() > self.settings.stale_after:
                        value["status"], value["error"] = "stale", "Datos desactualizados"
                        for key in ("cpu_percent", "temperature_c", "rx_mbps", "tx_mbps", "uptime_seconds"):
                            value[key] = None
                devices.append(value)
            summary = {key: sum(d["status"] == key for d in devices) for key in ("warning", "offline", "pending", "stale")}
            summary.update(total=len(devices), responding=sum(d["status"] in {"ok", "warning"} for d in devices))
            return {"mode": self.settings.mode, "generated_at": stamp(now), "last_poll_at": self.get_meta("last_poll_at"),
                    "devices": devices, "summary": summary}

    def history(self, device_id, hours=1):
        cutoff = stamp(utcnow() - timedelta(hours=hours))
        with self.lock:
            rows = self.db.execute("SELECT payload FROM samples WHERE device_id=? AND timestamp>=? ORDER BY timestamp",
                                   (device_id, cutoff)).fetchall()
            values = [json.loads(r[0]) for r in rows]
            # Never aggregate across a missing sample: preserve visible data gaps.
            return values

    def events(self, hours=24):
        cutoff = stamp(utcnow() - timedelta(hours=hours))
        with self.lock:
            rows = self.db.execute("SELECT * FROM events WHERE started_at>=? OR ended_at>=? OR ended_at IS NULL ORDER BY started_at DESC LIMIT 500",
                                   (cutoff, cutoff)).fetchall()
            return [dict(r) for r in rows]

    def get_meta(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key, value):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))
            self.db.commit()

    def prune(self):
        cutoff = stamp(utcnow() - timedelta(days=self.settings.retention_days))
        with self.lock:
            self.db.execute("DELETE FROM samples WHERE timestamp<?", (cutoff,))
            self.db.execute("DELETE FROM events WHERE ended_at IS NOT NULL AND ended_at<?", (cutoff,))
            self.db.commit()

    def demo_tick(self, now=None):
        """Datos sintéticos; no se realiza ninguna consulta SNMP en este modo."""
        now = now or utcnow()
        with self.lock:
            for i, device in enumerate(self.settings.devices):
                old = self._current(device["id"]) or self._blank(device)
                value = dict(old)
                down = i == 2
                value.update(status="offline" if down else "warning" if i == 1 else "ok",
                             last_attempt_at=stamp(now),
                             last_success_at=old.get("last_success_at") if down else stamp(now),
                             cpu_percent=None if down else round(30 + 12 * math.sin(now.timestamp() / 180 + i), 1),
                             temperature_c=None if down else 62 if i == 1 else 38,
                             rx_mbps=None if down else round(70 + 24 * math.sin(now.timestamp() / 110 + i), 2),
                             tx_mbps=None if down else round(12 + 5 * math.sin(now.timestamp() / 140 + i), 2),
                             uptime_seconds=None if down else 864000,
                             interface={"name": "Uplink de ejemplo", "index": None},
                             error="Ejemplo de pérdida de respuesta" if down else None)
                if down and not value["last_success_at"]:
                    value["last_success_at"] = stamp(now - timedelta(minutes=8))
                self._event_transition(old, value, now)
                self._save_current(value)
                self._sample(value, now)
            self.db.commit()
            self.set_meta("last_poll_at", stamp(now))

    def seed_demo(self):
        if self.settings.mode != "demo":
            return
        if not self.get_meta("demo_seeded"):
            now = utcnow()
            for minute in range(60, 0, -1):
                self.demo_tick(now - timedelta(minutes=minute))
            self.set_meta("demo_seeded", "true")
        self.demo_tick()

    def close(self):
        with self.lock:
            self.db.close()
