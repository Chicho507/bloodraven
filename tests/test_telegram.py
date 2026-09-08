"""Telegram tests use MockTransport exclusively; no real bot or network is used."""

import asyncio
import copy
import json
import logging
import unittest
from unittest.mock import patch

import httpx

from app.telegram_bot import (
    _ApiFailure,
    _SecretLogFilter,
    _authorized_request,
    _offset_key,
    _reply_call,
    format_command,
    run_bot,
)


TOKEN = "123456:FAKE_TEST_TOKEN_ONLY"
OVERVIEW = {
    "mode": "demo",
    "generated_at": "2026-09-07T18:00:00+00:00",
    "last_poll_at": "2026-09-07T17:59:40+00:00",
    "devices": [
        {"id": "alb-1", "name": "Switch Albrook", "site": "Albrook", "site_id": "alb",
         "site_aliases": ["Terminal"], "model": "Demo", "status": "ok", "cpu_percent": 18.5,
         "temperature_c": 38.0, "rx_mbps": 10.0, "tx_mbps": 3.0,
         "last_success_at": "2026-09-07T17:59:40Z", "interface": {"name": "uplink", "index": 1}},
        {"id": "cm-1", "name": "Switch Matriz", "site": "Casa Matriz", "status": "stale",
         "cpu_percent": 91.0, "rx_mbps": 400.0, "tx_mbps": 50.0,
         "last_success_at": "2026-09-07T16:00:00Z"},
    ],
    "summary": {"total": 2, "responding": 1, "warning": 0, "offline": 0, "pending": 0, "stale": 1},
}
EVENTS = [
    {"id": 1, "device_id": "cm-1", "device_name": "Switch Matriz", "site": "Casa Matriz",
     "kind": "offline", "started_at": "2026-09-07T15:00:00Z", "ended_at": None,
     "message": "Sin respuesta SNMP"},
]


def message(update_id=1, user_id=42, chat_id=42, chat_type="private", text="/estado"):
    return {"update_id": update_id, "message": {
        "from": {"id": user_id, "is_bot": False}, "chat": {"id": chat_id, "type": chat_type}, "text": text,
    }}


def callback(update_id=1, user_id=42, chat_id=42, chat_type="private", data="/estado"):
    return {"update_id": update_id, "callback_query": {
        "id": "callback-test", "from": {"id": user_id, "is_bot": False}, "data": data,
        "message": {"chat": {"id": chat_id, "type": chat_type}},
    }}


class FakeStore:
    def __init__(self):
        self.meta = {}
        self.saved = []
        self.snapshots = 0
        self.event_hours = []

    def snapshot(self):
        self.snapshots += 1
        return copy.deepcopy(OVERVIEW)

    def events(self, hours=24):
        self.event_hours.append(hours)
        return copy.deepcopy(EVENTS)

    def get_meta(self, key):
        return self.meta.get(key)

    def set_meta(self, key, value):
        self.meta[key] = value
        self.saved.append((key, value))


class FormatTests(unittest.TestCase):
    def test_demo_state_and_stale_are_explicit_and_pure(self):
        original = copy.deepcopy(OVERVIEW)
        result = format_command("/estado", OVERVIEW, EVENTS)
        self.assertIn("DEMO · Datos simulados", result)
        self.assertIn("Desactualizados 1", result)
        self.assertIn("Último ciclo: 07/09 17:59:40 UTC", result)
        self.assertEqual(OVERVIEW, original)
        self.assertEqual(result, format_command("/estado", OVERVIEW, EVENTS))

    def test_site_name_id_and_alias_casefold(self):
        for term in ["AlBrOoK", "ALB", "terminal"]:
            with self.subTest(term=term):
                result = format_command(f"/sucursal {term}", OVERVIEW, [])
                self.assertIn("Switch Albrook", result)
                self.assertNotIn("Switch Matriz", result)
                self.assertIn("CPU 18.5%", result)
                self.assertIn("entrada 10.0 Mbps", result)
                self.assertIn("no una prueba de velocidad", result)

    def test_colon_accepts_accents_and_phone_spelling(self):
        overview = copy.deepcopy(OVERVIEW)
        overview["devices"][0].update(site="Colón", name="SW-COLON")
        for term in ["colon", "COLÓN", "Colo\u0301n"]:
            with self.subTest(term=term):
                reply = format_command(f"/sucursal {term}", overview, [])
                self.assertIn("SW-COLON", reply)
                self.assertNotIn("No encuentro", reply)

    def test_nested_site_aliases_and_bot_command_suffix(self):
        overview = copy.deepcopy(OVERVIEW)
        overview["devices"][0]["site"] = {"id": "s-01", "name": "Albrook", "aliases": ["Norte"]}
        result = format_command("/sucursal@BloodRavenBot NORTE", overview, [])
        self.assertIn("Switch Albrook", result)

    def test_stale_values_are_not_presented_as_current(self):
        result = format_command("/sucursal Casa Matriz", OVERVIEW, [])
        self.assertIn("Datos desactualizados", result)
        self.assertIn("Valores no actuales", result)
        self.assertNotIn("400.0", result)
        self.assertNotIn("91.0", result)

    def test_missing_metrics_are_na_not_zero(self):
        overview = copy.deepcopy(OVERVIEW)
        overview["devices"][0].update(cpu_percent=None, temperature_c=None, rx_mbps=float("nan"), tx_mbps=None)
        result = format_command("/sucursal albrook", overview, [])
        self.assertIn("CPU N/D", result)
        self.assertIn("entrada N/D · salida N/D", result)
        self.assertNotIn("nan", result)

    def test_pending_and_offline_are_not_claimed_healthy(self):
        overview = copy.deepcopy(OVERVIEW)
        overview["mode"] = "live"
        overview["devices"][0]["status"] = "pending"
        overview["devices"][1]["status"] = "offline"
        result = format_command("/alertas", overview, [])
        self.assertIn("LIVE", result)
        self.assertIn("Sin primera lectura", result)
        self.assertIn("Sin respuesta SNMP", result)
        self.assertIn("no confirma que el equipo esté apagado", result)

    def test_no_devices_is_not_all_healthy(self):
        result = format_command("/alertas", {**OVERVIEW, "devices": []}, [])
        self.assertIn("Sin equipos configurados", result)
        self.assertNotIn("Sin alertas", result)

    def test_history_includes_overlapping_incident_not_old_closed(self):
        events = [*EVENTS,
                  {**EVENTS[0], "id": 2, "message": "abierto desde antes", "started_at": "2026-09-01T12:00:00Z"},
                  {**EVENTS[0], "id": 3, "message": "antiguo cerrado", "started_at": "2026-09-01T12:00:00Z", "ended_at": "2026-09-02T12:00:00Z"}]
        result = format_command("/historial 24h", OVERVIEW, events)
        self.assertIn("2 eventos", result)
        self.assertIn("abierto desde antes", result)
        self.assertNotIn("antiguo cerrado", result)
        self.assertIn("Abierto", result)

    def test_commands_help_and_unknown_site(self):
        self.assertIn("/historial 24h", format_command("/start", OVERVIEW, []))
        self.assertIn("Albrook", format_command("/sucursal", OVERVIEW, []))
        self.assertIn("No encuentro", format_command("/sucursal inexistente", OVERVIEW, []))
        self.assertIn("Usa /historial 24h", format_command("/historial 999h", OVERVIEW, []))

    def test_message_size_counts_surrogate_pairs(self):
        overview = {**OVERVIEW, "devices": [{**OVERVIEW["devices"][0], "name": "😀" * 100} for _ in range(200)]}
        result = format_command("/estado", overview, [])
        self.assertLessEqual(len(result.encode("utf-16-le")) // 2, 3900)
        self.assertIn("Resumen truncado", result)


class AuthorizationTests(unittest.TestCase):
    def test_allowlisted_user_own_private_chat(self):
        self.assertEqual(_authorized_request(message(), {42}), (42, "/estado", None))

    def test_user_group_other_chat_and_bot_rejected(self):
        invalid = [message(user_id=99, chat_id=99), message(chat_id=-123, chat_type="group"),
                   message(chat_id=99), message(user_id=True, chat_id=True), {"message": {}},
                   {"edited_message": message()["message"]}, {"message": {"text": "/estado"}}]
        bot = message()
        bot["message"]["from"]["is_bot"] = True
        invalid.append(bot)
        for update in invalid:
            with self.subTest(update=update):
                self.assertIsNone(_authorized_request(update, {42}))

    def test_callback_rechecks_user_chat_and_command(self):
        self.assertEqual(_authorized_request(callback(), {42}), (42, "/estado", "callback-test"))
        for update in [callback(user_id=99), callback(chat_id=-1, chat_type="supergroup"),
                       callback(chat_id=99), callback(data="/mutate"), callback(data={})]:
            with self.subTest(update=update):
                self.assertIsNone(_authorized_request(update, {42}))

    def test_http_log_filter_drops_secret_url(self):
        secret_filter = _SecretLogFilter(TOKEN)
        record = logging.LogRecord("httpx", logging.INFO, "", 0, "HTTP Request: %s", (f"https://api.telegram.org/bot{TOKEN}/getUpdates",), None)
        self.assertFalse(secret_filter.filter(record))
        clean = logging.LogRecord("httpx", logging.INFO, "", 0, "Safe unrelated message", (), None)
        self.assertTrue(secret_filter.filter(clean))


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def run_mock(self, store, stop, handler, allowed=None):
        real_client = httpx.AsyncClient
        with patch("app.telegram_bot.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)):
            await asyncio.wait_for(run_bot(store, stop, TOKEN, {42} if allowed is None else allowed), timeout=3)

    async def test_disabled_never_creates_client(self):
        with patch("app.telegram_bot.httpx.AsyncClient") as client:
            await run_bot(FakeStore(), asyncio.Event(), "", {42})
            await run_bot(FakeStore(), asyncio.Event(), TOKEN, set())
            client.assert_not_called()

    async def test_authorized_message_offset_persists_and_reloads(self):
        store, stop = FakeStore(), asyncio.Event()
        requests = []
        polls = 0

        async def handler(request):
            nonlocal polls
            body = json.loads(request.content)
            requests.append((request.url.path.rsplit("/", 1)[-1], body))
            if request.url.path.endswith("getUpdates"):
                polls += 1
                if polls == 1:
                    self.assertEqual(body["timeout"], 30)
                    self.assertEqual(body["allowed_updates"], ["message", "callback_query"])
                    return httpx.Response(200, json={"ok": True, "result": [message(update_id=10)]})
                self.assertEqual(body["offset"], 11)
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            self.assertNotIn("parse_mode", body)
            self.assertEqual(body["chat_id"], 42)
            self.assertEqual(sum(len(row) for row in body["reply_markup"]["inline_keyboard"]), 4)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        await self.run_mock(store, stop, handler)
        self.assertEqual(store.meta[_offset_key(TOKEN)], "11")
        self.assertEqual(len([r for r in requests if r[0] == "sendMessage"]), 1)
        self.assertNotIn(TOKEN, str(store.meta))
        next_stop = asyncio.Event()

        async def restarted(request):
            self.assertEqual(json.loads(request.content)["offset"], 11)
            next_stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})

        await self.run_mock(store, next_stop, restarted)

    async def test_unauthorized_messages_and_callbacks_receive_nothing(self):
        store, stop = FakeStore(), asyncio.Event()
        polls = 0

        async def handler(request):
            nonlocal polls
            self.assertTrue(request.url.path.endswith("getUpdates"))
            polls += 1
            if polls == 1:
                updates = [message(1, 99, 99), message(2, 42, -100, "group"),
                           callback(3, 99, 99), callback(4, 42, -100, "group"), callback(5, 42, 99)]
                return httpx.Response(200, json={"ok": True, "result": updates})
            stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})

        await self.run_mock(store, stop, handler)
        self.assertEqual(store.snapshots, 0)
        self.assertEqual(store.meta[_offset_key(TOKEN)], "6")

    async def test_authorized_history_callback_is_answered_and_reads_events(self):
        store, stop = FakeStore(), asyncio.Event()
        methods = []

        async def handler(request):
            method = request.url.path.rsplit("/", 1)[-1]
            methods.append(method)
            if method == "getUpdates":
                if methods.count(method) == 1:
                    return httpx.Response(200, json={"ok": True, "result": [callback(data="/historial 24h")]})
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": True})

        await self.run_mock(store, stop, handler)
        self.assertEqual(methods, ["getUpdates", "answerCallbackQuery", "sendMessage", "getUpdates"])
        self.assertEqual(store.event_hours, [24])

    async def test_poll_429_respects_retry_after(self):
        store, stop = FakeStore(), asyncio.Event()
        calls, delays = 0, []

        async def pause(event, delay):
            delays.append(delay)
            return False

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 7}})
            stop.set()
            return httpx.Response(200, json={"ok": True, "result": []})

        with patch("app.telegram_bot._pause", side_effect=pause):
            await self.run_mock(store, stop, handler)
        self.assertEqual(delays, [7.0])

    async def test_network_backoff_is_bounded_and_can_stop(self):
        store, stop = FakeStore(), asyncio.Event()
        delays = []

        async def pause(event, delay):
            delays.append(delay)
            if len(delays) == 7:
                event.set()
                return True
            return False

        async def handler(request):
            raise httpx.ConnectError("simulated transport failure", request=request)

        with patch("app.telegram_bot._pause", side_effect=pause):
            await self.run_mock(store, stop, handler)
        self.assertEqual(delays, [1, 2, 4, 8, 16, 30, 30])

    async def test_stop_cancels_inflight_long_poll(self):
        store, stop = FakeStore(), asyncio.Event()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(self.run_mock(store, stop, handler))
        await asyncio.wait_for(entered.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())

    async def test_send_failure_is_bounded_and_offset_still_advances(self):
        store, stop = FakeStore(), asyncio.Event()
        polls, sends = 0, 0

        async def pause(event, delay):
            return False

        async def handler(request):
            nonlocal polls, sends
            if request.url.path.endswith("getUpdates"):
                polls += 1
                if polls == 1:
                    return httpx.Response(200, json={"ok": True, "result": [message(update_id=8)]})
                self.assertEqual(json.loads(request.content)["offset"], 9)
                stop.set()
                return httpx.Response(200, json={"ok": True, "result": []})
            sends += 1
            return httpx.Response(503, json={"ok": False, "error_code": 503})

        with patch("app.telegram_bot._pause", side_effect=pause):
            await self.run_mock(store, stop, handler)
        self.assertEqual(sends, 3)
        self.assertEqual(store.meta[_offset_key(TOKEN)], "9")

    async def test_blocked_user_response_is_not_retried(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(403, json={"ok": False, "error_code": 403})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(_ApiFailure):
                await _reply_call(client, "https://example.test/sendMessage", {"text": "test"}, asyncio.Event())
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
