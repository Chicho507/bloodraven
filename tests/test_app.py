from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import Store, rates, stamp, utcnow


DEVICE = {"id": "albrook", "name": "SW-ALBROOK", "site": "Albrook", "model": "CBS350-48FP-4G",
          "profile": "cisco_cbs350", "enabled": True, "warn_temperature_c": 60}
PASSWORD = "only-for-tests-never-deploy"


def settings(directory, mode="live"):
    return Settings(mode, "tester", PASSWORD, str(Path(directory) / f"{mode}.sqlite3"), [DEVICE], telegram_allowed_ids=set())


def sample(**changes):
    return {"ok": True, "cpu_percent": 30, "temperature_c": 38, "uptime_seconds": 1000,
            "in_octets": 1000000, "out_octets": 2000000, "interface_index": 49, "interface_name": "gi49",
            "counter_discontinuity": 0, "error": None, **changes}


class RateTests(unittest.TestCase):
    def test_rate_uses_elapsed_time_and_64_bit_integer_counters(self):
        now = utcnow()
        previous = {**sample(in_octets=2**60, out_octets=2**60), "timestamp": stamp(now - timedelta(seconds=30))}
        now = now.replace(microsecond=0)
        self.assertEqual(rates(previous, sample(in_octets=2**60 + 3750000, out_octets=2**60 + 7500000), now), (1.0, 2.0))

    def test_restart_and_discontinuity_do_not_produce_spikes(self):
        now = utcnow()
        previous = {**sample(), "timestamp": stamp(now - timedelta(seconds=30))}
        for changes in ({"uptime_seconds": 5}, {"counter_discontinuity": 10}, {"interface_index": 50}):
            with self.subTest(changes=changes):
                self.assertEqual(rates(previous, sample(**changes), now), (None, None))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(settings(self.directory.name))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_failed_poll_removes_values_and_incident_opens_after_three_failures(self):
        now = utcnow()
        self.store.record(DEVICE, sample(), now)
        for n in range(1, 4):
            self.store.record(DEVICE, {"ok": False, "error": "Sin respuesta"}, now + timedelta(seconds=n))
        device = self.store.snapshot()["devices"][0]
        self.assertEqual(device["status"], "offline")
        self.assertIsNone(device["cpu_percent"])
        self.assertEqual(len(self.store.events()), 1)
        self.store.record(DEVICE, sample(uptime_seconds=1100), now + timedelta(seconds=30))
        self.assertIsNotNone(self.store.events()[0]["ended_at"])
        self.assertIsNone(self.store.snapshot()["devices"][0]["rx_mbps"])

    def test_old_success_is_stale_without_claiming_zero(self):
        self.store.record(DEVICE, sample(), utcnow() - timedelta(minutes=10))
        result = self.store.snapshot()
        self.assertEqual(result["summary"]["stale"], 1)
        self.assertIsNone(result["devices"][0]["temperature_c"])

    def test_missing_configuration_is_pending_not_offline(self):
        self.store.pending(DEVICE, "Revisión de firmware pendiente")
        self.assertEqual(self.store.snapshot()["devices"][0]["status"], "pending")
        self.assertEqual(self.store.events(), [])

    def test_open_incident_is_kept_after_history_window(self):
        for n in range(3):
            self.store.record(DEVICE, {"ok": False}, utcnow() - timedelta(days=2, seconds=n))
        self.assertEqual(len(self.store.events(hours=24)), 1)


class APITests(unittest.TestCase):
    def test_auth_protects_both_inventory_and_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(settings(directory), start_workers=False)) as client:
                self.assertEqual(client.get("/healthz").status_code, 200)
                for path in ("/", "/api/overview", "/static/app.js", "/api/events"):
                    self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.get("/api/overview", headers={"Authorization": "Basic %invalid"}).status_code, 401)
                response = client.get("/api/overview", auth=("tester", PASSWORD))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("_counter", response.json()["devices"][0])
                self.assertNotIn("snmp", response.json()["devices"][0])

    def test_demo_bot_and_history_use_same_store_and_live_simulator_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            with TestClient(create_app(settings(directory, "demo"), start_workers=False)) as client:
                client.auth = ("tester", PASSWORD)
                response = client.post("/api/demo/telegram", json={"command": "/sucursal albrook"})
                self.assertEqual(response.status_code, 200)
                self.assertIn("DEMO", response.json()["reply"])
                self.assertIn("SW-ALBROOK", response.json()["reply"])
                self.assertTrue(client.get("/api/devices/albrook/history").json()["samples"])
                self.assertEqual(client.get("/api/devices/unknown/history").status_code, 404)
                self.assertEqual(client.get("/api/devices/albrook/history?hours=900").status_code, 422)
            with TestClient(create_app(settings(directory), start_workers=False)) as client:
                self.assertEqual(client.post("/api/demo/telegram", auth=("tester", PASSWORD), json={"command": "/estado"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
