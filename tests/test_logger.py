from fixtures import ADDRESS, PIN, DEVICE_SUFFIX, MQTT_ID
import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spc_bluetooth"))
from decoder import decode, uuid, SERVICE, PV_SERVICE
from poller import Storage, sample, inventory, PairingAgent
from dbus_fast import DBusError, Variant
from web import start_server


def ac_packet():
    # Known engineering values: grid 230.1V, 50Hz; output 229.8V, 50Hz;
    # 1500VA, 1234W, 25% load, unused word, battery 51.23V, charging 17A.
    return struct.pack("<10H", 2301, 500, 2298, 500, 1500, 1234, 25, 0, 5123, 17)


def status_packet():
    return struct.pack("<6H", 78, 0, 9, 0, 1, 0) + b"B"


def pv_packet():
    return b"00001.00" + bytes(4) + struct.pack("<2H", 3215, 2345)


class DecoderTests(unittest.TestCase):
    def test_ac_units_and_offsets(self):
        values = decode(uuid("2a03"), ac_packet())
        self.assertEqual(values["grid_voltage_v"], 230.1)
        self.assertEqual(values["output_voltage_v"], 229.8)
        self.assertEqual(values["output_frequency_hz"], 50)
        self.assertEqual(values["output_apparent_power_va"], 1500)
        self.assertEqual(values["output_power_w"], 1234)
        self.assertEqual(values["battery_voltage_v"], 51.23)
        self.assertEqual(values["battery_charge_current_a"], 17)

    def test_status_and_pv(self):
        values = decode(uuid("2a04"), status_packet())
        self.assertEqual(values["battery_capacity_percent"], 78)
        self.assertEqual(values["battery_discharge_current_a"], 9)
        self.assertEqual(values["operating_mode_code"], "B")
        self.assertEqual(values["event_flags_hex"], "01000000")
        self.assertEqual(decode(uuid("2a11"), pv_packet()),
                         {"pv_voltage_v": 321.5, "pv_power_w": 2345})

    def test_rejects_short_and_invalid_data(self):
        for short, packet in (("2a03", ac_packet()), ("2a04", status_packet()), ("2a11", pv_packet())):
            for size in range(len(packet)):
                with self.subTest(short=short, size=size), self.assertRaises(ValueError):
                    decode(uuid(short), packet[:size])
        with self.assertRaises(ValueError):
            decode(uuid("2a03"), b"\xff\xff" + ac_packet()[2:])
        with self.assertRaises(ValueError):
            decode(uuid("2a04"), b"\x65\x00" + status_packet()[2:])

    def test_captured_spc_packet(self):
        # Real read from SPC III 5000-48; PV characteristic is in service 1811.
        ac = bytes.fromhex("d808f401ff08f401e600970004007201aa140c00")
        pv = bytes.fromhex("2d2d2d2d2d2e2d2d20203b00a306fa0300000000")
        self.assertEqual(decode(uuid("2a03"), ac)["battery_voltage_v"], 52.9)
        self.assertEqual(decode(uuid("2a03"), ac)["output_power_w"], 151)
        self.assertEqual(decode(uuid("2a11"), pv)["pv_power_w"], 1018)
        self.assertEqual(decode(uuid("2a11"), pv)["pv_voltage_v"], 169.9)

    def test_zero_is_valid(self):
        self.assertEqual(decode(uuid("2a11"), bytes(16))["pv_power_w"], 0)


class PollTests(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [{"path": "/" + key, "uuid": uuid(key),
                 "service": PV_SERVICE if key == "2a11" else SERVICE, "flags": ["read"]}
                for key in ("2a03", "2a04", "2a11")]

    async def test_read_only_calls_and_good_sample(self):
        mock = AsyncMock(side_effect=[[ac_packet()], [status_packet()], [pv_packet()]])
        with patch("poller.call", mock):
            result = await sample(None, self.rows(), False)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["protocol_validated"])
        self.assertEqual(result["metrics"]["pv_power_w"], 2345)
        self.assertEqual(len(result["raw"]), 3)
        for call in mock.call_args_list:
            self.assertEqual(call.args[3], "ReadValue")

    async def test_failure_never_reuses_previous_readings(self):
        with patch("poller.call", AsyncMock(side_effect=[[ac_packet()], [status_packet()], [pv_packet()]])):
            first = await sample(None, self.rows(), False)
        with patch("poller.call", AsyncMock(side_effect=[TimeoutError(), [status_packet()], [pv_packet()]])):
            second = await sample(None, self.rows(), False)
        self.assertIn("battery_voltage_v", first["metrics"])
        self.assertNotIn("battery_voltage_v", second["metrics"])
        self.assertEqual(second["status"], "partial")

    async def test_wrong_service_not_read_or_decoded(self):
        rows = self.rows()
        for row in rows:
            row["service"] = uuid("1800")
        mock = AsyncMock()
        with patch("poller.call", mock):
            result = await sample(None, rows, True)
        mock.assert_not_called()
        self.assertEqual(result["status"], "unavailable")

    async def test_duplicate_not_decoded(self):
        rows = [self.rows()[0], dict(self.rows()[0], path="/duplicate")]
        with patch("poller.call", AsyncMock(return_value=[ac_packet()])):
            result = await sample(None, rows, False)
        self.assertEqual(result["metrics"], {})
        self.assertEqual(len(result["raw"]), 2)

    async def test_short_packet_kept_raw(self):
        with patch("poller.call", AsyncMock(return_value=[b"\x01"])):
            result = await sample(None, self.rows(), False)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(all(value["hex"] == "01" for value in result["raw"].values()))

    def test_inventory_ignores_other_devices(self):
        target = "/org/bluez/hci0/dev_AA"
        service = target + "/service1"
        tree = {
            service: {"org.bluez.GattService1": {"UUID": Variant("s", SERVICE)}},
            service + "/char1": {"org.bluez.GattCharacteristic1": {
                "UUID": Variant("s", uuid("2a03")), "Service": Variant("o", service),
                "Flags": Variant("as", ["read"])}},
            "/org/bluez/hci0/dev_BB/service1/char1": {"org.bluez.GattCharacteristic1": {}},
        }
        rows = inventory(tree, target)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], SERVICE)


class StorageAndWebTests(unittest.TestCase):
    def test_csv_blanks_and_retention(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(directory, 2)
            base = Path(directory)
            (base / "metrics-2026-09-20.csv").write_text("old")
            (base / "raw-2026-09-20.jsonl").write_text("old")
            (base / "metrics-2026-09-24.csv").write_text("keep")
            (base / "notes.txt").write_text("keep")
            good = {"timestamp": "2026-09-25T12:00:00+00:00", "status": "ok",
                    "metrics": {"battery_voltage_v": 51.23}, "raw": {}, "errors": []}
            store.save(good)
            store.save(dict(good, status="unavailable", metrics={}))
            with (base / "metrics-2026-09-25.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["battery_voltage_v"], "51.23")
            self.assertEqual(rows[1]["battery_voltage_v"], "")
            self.assertEqual(json.loads((base / "latest.json").read_text())["metrics"], {})
            self.assertFalse((base / "metrics-2026-09-20.csv").exists())
            self.assertFalse((base / "raw-2026-09-20.jsonl").exists())
            self.assertTrue((base / "metrics-2026-09-24.csv").exists())
            self.assertTrue((base / "notes.txt").exists())

    def test_ingress_page_downloads_escape_and_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data = {"status": "partial", "metrics": {"x": "<script>alert(1)</script>"},
                    "errors": ["<bad>"], "timestamp": datetime.now(timezone.utc).isoformat()}
            (base / "latest.json").write_text(json.dumps(data))
            server = start_server(base, port=0)
            url = f"http://127.0.0.1:{server.server_port}"
            try:
                page = urlopen(url).read().decode()
                self.assertIn("&lt;script&gt;", page)
                self.assertNotIn("<script>", page)
                self.assertEqual(json.loads(urlopen(url + "/api/latest").read()), data)
                self.assertEqual(json.loads(urlopen(url + "/files/latest.json").read()), data)
                for path in ("/files/../config.yaml", "/files/%2e%2e/config.yaml", "/files/options.json"):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(url + path)
                    self.assertEqual(error.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()

    def test_agent_restricts_device(self):
        agent = PairingAgent("/org/bluez/hci0/dev_AA", PIN)
        agent.check("/org/bluez/hci0/dev_AA")
        with self.assertRaises(DBusError):
            agent.check("/org/bluez/hci0/dev_BB")


if __name__ == "__main__":
    unittest.main()
