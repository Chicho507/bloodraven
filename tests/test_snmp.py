"""Exercise the actual polling flow with mocked transport; never contact devices."""

import asyncio
import copy
import os
import unittest
from unittest.mock import AsyncMock, patch

from pysnmp.proto.rfc1902 import Counter64, Integer32, OctetString, TimeTicks
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject

from app import snmp


DEVICE = {
    "id": "test-switch",
    "host": "192.0.2.10",  # RFC 5737 documentation address; never contacted.
    "port": 161,
    "profile": "cisco_cbs350",
    "interface_index": 49,
    "snmp": {
        "username_env": "BR_TEST_SNMP_USER",
        "auth_password_env": "BR_TEST_SNMP_AUTH",
        "privacy_password_env": "BR_TEST_SNMP_PRIV",
        "auth_protocol": "SHA256",
        "privacy_protocol": "AES128",
    },
}
ENV = {
    "BR_TEST_SNMP_USER": "test-monitor",
    "BR_TEST_SNMP_AUTH": "test-auth-passphrase",
    "BR_TEST_SNMP_PRIV": "test-priv-passphrase",
}


def response(values):
    return None, 0, 0, list(values.items())


class PollDeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.device = copy.deepcopy(DEVICE)
        self.environment = patch.dict(os.environ, ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.engine_patch = patch.object(snmp, "SnmpEngine")
        self.engine_factory = self.engine_patch.start()
        self.addCleanup(self.engine_patch.stop)
        self.transport_patch = patch.object(snmp.UdpTransportTarget, "create", new_callable=AsyncMock)
        self.transport = self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        self.get_patch = patch.object(snmp, "get_cmd", new_callable=AsyncMock)
        self.get = self.get_patch.start()
        self.addCleanup(self.get_patch.stop)
        # Object identity resolution is normally done within get_cmd. Keep
        # readable numeric OIDs in the fake command so its boundaries are tested.
        self.identity_patch = patch.object(snmp, "ObjectIdentity", side_effect=lambda oid: oid)
        self.identity_patch.start()
        self.addCleanup(self.identity_patch.stop)
        self.object_patch = patch.object(snmp, "ObjectType", side_effect=lambda oid: oid)
        self.object_patch.start()
        self.addCleanup(self.object_patch.stop)

    def queue_readings(self, metrics=None, uptime=TimeTicks(123456)):
        self.get.side_effect = [
            response({snmp.SYS_UPTIME: uptime}),
            response(metrics or {}),
        ]

    async def test_valid_snapshot_preserves_64_bit_counters_and_closes_engine(self):
        counter = 2**63 + 123
        self.queue_readings({
            snmp.CBS_CPU_ONE_MINUTE: Integer32(42),
            snmp.CBS_TEMPERATURE: Integer32(38),
            f"{snmp.IF_HC_IN_OCTETS}.49": Counter64(counter),
            f"{snmp.IF_HC_OUT_OCTETS}.49": Counter64(654321),
            f"{snmp.IF_NAME}.49": OctetString("gi49"),
            f"{snmp.IF_COUNTER_DISCONTINUITY}.49": TimeTicks(54321),
        })
        result = await snmp.poll_device(self.device)
        self.assertEqual(result, {
            "ok": True,
            "cpu_percent": 42.0,
            "temperature_c": 38.0,
            "uptime_seconds": 1234.56,
            "in_octets": counter,
            "out_octets": 654321,
            "interface_name": "gi49",
            "interface_index": 49,
            "counter_discontinuity": 54321,
            "error": None,
        })
        self.transport.assert_awaited_once_with(
            ("192.0.2.10", 161), timeout=2.0, retries=1,
        )
        calls = self.get.await_args_list
        self.assertEqual(calls[0].args[4:], (snmp.SYS_UPTIME,))
        self.assertEqual(calls[1].args[4:], (
            snmp.CBS_CPU_ONE_MINUTE, snmp.CBS_TEMPERATURE,
            f"{snmp.IF_HC_IN_OCTETS}.49", f"{snmp.IF_HC_OUT_OCTETS}.49", f"{snmp.IF_NAME}.49",
            f"{snmp.IF_COUNTER_DISCONTINUITY}.49",
        ))
        self.assertEqual(calls[0].kwargs, {"lookupMib": False})
        credentials = calls[0].args[1]
        self.assertEqual(credentials.authentication_protocol, snmp.USM_AUTH_HMAC192_SHA256)
        self.assertEqual(credentials.privacy_protocol, snmp.USM_PRIV_CFB128_AES)
        self.engine_factory.return_value.close_dispatcher.assert_called_once()

    async def test_generic_profiles_only_query_standard_interface_objects(self):
        for profile in ("cisco_generic", "dell_generic", "unifi_generic"):
            with self.subTest(profile=profile):
                self.get.reset_mock()
                self.device["profile"] = profile
                self.queue_readings({f"{snmp.IF_HC_IN_OCTETS}.49": Counter64(123), f"{snmp.IF_HC_OUT_OCTETS}.49": Counter64(456)})
                result = await snmp.poll_device(self.device)
                self.assertTrue(result["ok"])
                self.assertEqual(result["in_octets"], 123)
                self.assertIsNone(result["cpu_percent"])
                self.assertIsNone(result["temperature_c"])
                requested = [oid for call in self.get.await_args_list for oid in call.args[4:]]
                self.assertTrue(all(oid.startswith("1.3.6.1.2.1.") for oid in requested))

    async def test_encrypted_vault_values_work_without_environment_credentials(self):
        self.device["_credentials"] = ["vault-monitor", "vault-auth-passphrase", "vault-priv-passphrase"]
        with patch.dict(os.environ, {}, clear=True):
            self.queue_readings()
            result = await snmp.poll_device(self.device)
        self.assertTrue(result["ok"])
        self.assertNotIn("vault-", str(result))

    async def test_missing_optional_objects_remain_null_while_zero_uptime_is_valid(self):
        self.queue_readings({
            snmp.CBS_CPU_ONE_MINUTE: NoSuchInstance(""),
            snmp.CBS_TEMPERATURE: NoSuchObject(""),
            f"{snmp.IF_HC_IN_OCTETS}.49": NoSuchInstance(""),
        }, uptime=TimeTicks(0))
        result = await snmp.poll_device(self.device)
        self.assertTrue(result["ok"])
        self.assertEqual(result["uptime_seconds"], 0)
        for field in ("cpu_percent", "temperature_c", "in_octets", "out_octets", "interface_name", "counter_discontinuity"):
            self.assertIsNone(result[field])

    async def test_invalid_and_negative_counters_are_not_coerced_to_zero(self):
        self.queue_readings({
            snmp.CBS_CPU_ONE_MINUTE: Integer32(101),
            f"{snmp.IF_HC_IN_OCTETS}.49": Integer32(-1),
            f"{snmp.IF_HC_OUT_OCTETS}.49": "1.5",
        })
        result = await snmp.poll_device(self.device)
        for field in ("cpu_percent", "in_octets", "out_octets"):
            self.assertIsNone(result[field])

    async def test_missing_uptime_does_not_report_success_or_query_optional_objects(self):
        self.queue_readings(uptime=NoSuchInstance(""))
        result = await snmp.poll_device(self.device)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["uptime_seconds"])
        self.assertIn("sysUpTime", result["error"])
        self.get.assert_awaited_once()

    async def test_sg350_requires_reviewed_firmware_without_network_or_engine(self):
        self.device["profile"] = "cisco_sg350"
        for firmware, reviewed in ((None, False), ("2.5.9.54", False), (None, True), ("2.5.9.54", "true")):
            with self.subTest(firmware=firmware, reviewed=reviewed):
                self.device.update(firmware=firmware, firmware_reviewed=reviewed)
                result = await snmp.poll_device(self.device)
                self.assertFalse(result["ok"])
                self.assertIn("firmware", result["error"])
        self.engine_factory.assert_not_called()
        self.transport.assert_not_awaited()
        self.get.assert_not_awaited()

    async def test_reviewed_sg350_without_interface_only_reads_uptime(self):
        self.device.update(profile="cisco_sg350", firmware="reviewed-version", firmware_reviewed=True, interface_index=None)
        self.queue_readings()
        result = await snmp.poll_device(self.device)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["cpu_percent"])
        self.assertIsNone(result["temperature_c"])
        self.assertIsNone(result["interface_index"])
        self.get.assert_awaited_once()

    async def test_missing_interface_does_not_enumerate_or_guess_ports(self):
        self.device["interface_index"] = None
        self.queue_readings()
        result = await snmp.poll_device(self.device)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["in_octets"])
        self.assertEqual(self.get.await_args_list[1].args[4:], (snmp.CBS_CPU_ONE_MINUTE, snmp.CBS_TEMPERATURE))

    async def test_invalid_credentials_are_rejected_before_transport(self):
        variants = (
            {"auth_protocol": "MD5"},
            {"privacy_protocol": "DES"},
            {"username_env": "not a variable name"},
            {"auth_password_env": "BR_TEST_UNDEFINED"},
        )
        with patch.dict(os.environ, {"BR_TEST_UNDEFINED": ""}):
            for change in variants:
                with self.subTest(change=change):
                    self.device["snmp"] = {**DEVICE["snmp"], **change}
                    result = await snmp.poll_device(self.device)
                    self.assertFalse(result["ok"])
                    self.assertIn("Configuración", result["error"])
        self.transport.assert_not_awaited()

    async def test_short_passphrase_and_boolean_interface_are_rejected(self):
        with patch.dict(os.environ, {"BR_TEST_SNMP_AUTH": "short"}):
            self.assertFalse((await snmp.poll_device(self.device))["ok"])
        self.device["interface_index"] = True
        self.assertFalse((await snmp.poll_device(self.device))["ok"])
        self.transport.assert_not_awaited()

    async def test_timeout_is_bounded_and_closes_dispatcher(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        self.transport.side_effect = stalled
        with patch.object(snmp, "POLL_TIMEOUT_SECONDS", 0.01):
            result = await snmp.poll_device(self.device)
        self.assertFalse(result["ok"])
        self.assertIn("tiempo", result["error"])
        self.engine_factory.return_value.close_dispatcher.assert_called_once()

    async def test_network_exception_does_not_expose_credentials(self):
        self.get.side_effect = RuntimeError(ENV["BR_TEST_SNMP_AUTH"])
        result = await snmp.poll_device(self.device)
        self.assertFalse(result["ok"])
        self.assertNotIn(ENV["BR_TEST_SNMP_AUTH"], str(result))
        self.engine_factory.return_value.close_dispatcher.assert_called_once()

    async def test_optional_pdu_error_keeps_confirmed_reachability(self):
        self.get.side_effect = [response({snmp.SYS_UPTIME: TimeTicks(100)}), (None, 5, 1, [])]
        result = await snmp.poll_device(self.device)
        self.assertTrue(result["ok"])
        self.assertEqual(result["uptime_seconds"], 1.0)
        self.assertIsNone(result["cpu_percent"])
        self.assertIn("algunas métricas", result["error"])

    async def test_cancellation_propagates_and_closes_dispatcher(self):
        self.get.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await snmp.poll_device(self.device)
        self.engine_factory.return_value.close_dispatcher.assert_called_once()


if __name__ == "__main__":
    unittest.main()
