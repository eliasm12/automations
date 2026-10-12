from fixtures import ADDRESS, PIN, DEVICE_SUFFIX, MQTT_ID
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from io import BytesIO

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spc_bluetooth"))
from decoder import METRICS
from mqtt_bridge import Publisher, SENSORS, supervisor_service
from controls import SELECTS


class MqttTests(unittest.TestCase):
    def test_every_decoded_field_has_sensor_metadata(self):
        self.assertEqual(set(METRICS), set(SENSORS))
        publisher = Publisher(ADDRESS)
        self.assertEqual(publisher.config("pv_power_w")["device_class"], "power")
        self.assertEqual(publisher.config("pv_power_w")["unit_of_measurement"], "W")
        self.assertEqual(publisher.config("pv_power_w")["state_class"], "measurement")
        self.assertNotIn("state_class", publisher.config("operating_mode_code"))
        self.assertEqual(publisher.config("pv_power_w")["availability_mode"], "all")
        self.assertEqual(publisher.config("pv_power_w")["expire_after"], 180)

    def test_discovery_and_missing_readings(self):
        publisher = Publisher(ADDRESS)
        client = MagicMock()
        client.is_connected.return_value = True
        publisher.client = client
        publisher.on_connect(client, None, None, 0, None)
        retained_config = [c for c in client.publish.call_args_list if c.args[0].endswith("/config") and c.args[1]]
        self.assertEqual(len(retained_config), len(METRICS))
        self.assertTrue(all(c.kwargs["retain"] for c in retained_config))
        self.assertEqual(json.loads(retained_config[0].args[1])["device"]["identifiers"],
                         [f"spc_{MQTT_ID}"])
        client.publish.reset_mock()
        publisher.publish_sample({"metrics": {"pv_power_w": 1018, "battery_voltage_v": 52.9}})
        published = {(c.args[0], c.args[1]) for c in client.publish.call_args_list}
        self.assertIn((f"{publisher.base}/pv_power_w/state", "1018"), published)
        self.assertIn((f"{publisher.base}/pv_power_w/available", "online"), published)
        self.assertIn((f"{publisher.base}/output_power_w/available", "offline"), published)
        client.publish.reset_mock()
        publisher.publish_sample({"metrics": {}})
        topics = {c.args[0] for c in client.publish.call_args_list}
        # Missing fields never seen are offline; recent PV/battery stay unchanged.
        self.assertEqual(len(topics), len(METRICS) - 2)
        self.assertNotIn(f"{publisher.base}/pv_power_w/available", topics)
        self.assertNotIn(f"{publisher.base}/battery_voltage_v/available", topics)
        self.assertTrue(all(topic.endswith("/available") for topic in topics))

    def test_flaps_expire_from_last_real_reading_and_recover(self):
        clock = MagicMock(return_value=0.0)
        publisher = Publisher(ADDRESS, stale_seconds=90, clock=clock)
        client = MagicMock()
        client.is_connected.return_value = True
        publisher.client = client
        self.assertEqual(publisher.config("pv_power_w")["expire_after"], 90)
        publisher.publish_sample({"metrics": {key: 0 for key in SENSORS}})
        self.assertEqual(publisher.last_seen["pv_power_w"], 0.0)
        for seconds in (20, 40, 89):
            with self.subTest(seconds=seconds):
                clock.return_value = seconds
                client.publish.reset_mock()
                publisher.publish_sample({"metrics": {}})
                # No offline messages, state replays, or freshness extension.
                client.publish.assert_not_called()
                self.assertEqual(publisher.last_seen["pv_power_w"], 0.0)
        clock.return_value = 90
        publisher.publish_sample({"metrics": {}})
        self.assertEqual(client.publish.call_count, len(SENSORS))
        self.assertTrue(all(c.args[1] == "offline" for c in client.publish.call_args_list))
        clock.return_value = 100
        client.publish.reset_mock()
        publisher.publish_sample({"metrics": {"pv_power_w": 500}})
        messages = {(c.args[0], c.args[1]) for c in client.publish.call_args_list}
        self.assertIn((f"{publisher.base}/pv_power_w/state", "500"), messages)
        self.assertIn((f"{publisher.base}/pv_power_w/available", "online"), messages)
        self.assertEqual(publisher.last_seen["pv_power_w"], 100)

    def test_partial_reads_do_not_refresh_unrelated_fields(self):
        clock = MagicMock(return_value=0.0)
        publisher = Publisher(ADDRESS, stale_seconds=60, clock=clock)
        client = MagicMock()
        client.is_connected.return_value = True
        publisher.client = client
        publisher.publish_sample({"metrics": {"pv_power_w": 800, "battery_voltage_v": 52}})
        clock.return_value = 50
        publisher.publish_sample({"metrics": {"battery_voltage_v": 53}})
        clock.return_value = 60
        client.publish.reset_mock()
        publisher.publish_sample({"metrics": {}})
        messages = {(c.args[0], c.args[1]) for c in client.publish.call_args_list}
        self.assertIn((f"{publisher.base}/pv_power_w/available", "offline"), messages)
        self.assertNotIn((f"{publisher.base}/battery_voltage_v/available", "offline"), messages)
        self.assertTrue(all(c.args[0].endswith("/available") for c in client.publish.call_args_list))

    def test_mqtt_reconnect_keeps_only_recent_availability(self):
        clock = MagicMock(return_value=0.0)
        publisher = Publisher(ADDRESS, stale_seconds=60, clock=clock)
        client = MagicMock()
        client.is_connected.return_value = True
        publisher.client = client
        publisher.on_connect(client, None, None, 0, None)
        messages = {(c.args[0], c.args[1]) for c in client.publish.call_args_list}
        self.assertIn((f"{publisher.base}/pv_power_w/available", "offline"), messages)
        publisher.publish_sample({"metrics": {"pv_power_w": 800}})
        for seconds, availability in ((30, "online"), (60, "offline")):
            clock.return_value = seconds
            client.publish.reset_mock()
            publisher.on_connect(client, None, None, 0, None)
            messages = {(c.args[0], c.args[1]) for c in client.publish.call_args_list}
            self.assertIn((f"{publisher.base}/pv_power_w/available", availability), messages)
            self.assertIn((f"{publisher.base}/output_power_w/available", "offline"), messages)
            self.assertFalse(any(c.args[0].endswith("/state") for c in client.publish.call_args_list))

    def test_ha_restart_republishes_discovery(self):
        publisher = Publisher(ADDRESS)
        client = MagicMock()
        message = MagicMock(topic="homeassistant/status", payload=b"online")
        publisher.on_message(client, None, message)
        self.assertEqual(client.publish.call_count, len(METRICS) + len(SELECTS))

    def test_selects_are_confirmed_not_optimistic_and_removed_when_disabled(self):
        publisher = Publisher(ADDRESS, allow_control=True)
        config = publisher.select_config("output_source_priority")
        self.assertEqual(config["options"], ["USB", "SUB", "SBU"])
        self.assertFalse(config["retain"])
        self.assertFalse(config["optimistic"])
        self.assertEqual(config["availability_mode"], "all")
        publisher.allow_control = False
        client = MagicMock()
        publisher.discovery(client)
        removals = [c for c in client.publish.call_args_list if "/select/" in c.args[0]]
        self.assertEqual(len(removals), 2)
        self.assertTrue(all(c.args[1] == "" and c.kwargs["retain"] for c in removals))

    def test_commands_require_fresh_live_read_and_do_not_optimistically_change_state(self):
        clock = MagicMock(return_value=0)
        publisher = Publisher(ADDRESS, allow_control=True, clock=clock)
        client = MagicMock()
        client.is_connected.return_value = True
        publisher.client = client
        message = MagicMock(topic=f"{publisher.base}/output_source_priority/set", payload=b"USB", retain=False)
        publisher.on_message(client, None, message)
        self.assertIsNone(publisher.take_command())
        publisher.publish_controls({"output_source_priority": "SBU", "charger_source_priority": "Solar only"})
        client.publish.reset_mock()
        publisher.on_message(client, None, message)
        client.publish.assert_not_called()
        self.assertEqual(publisher.take_command(), ("output_source_priority", "USB"))
        message.retain = True
        publisher.on_message(client, None, message)
        self.assertIsNone(publisher.take_command())
        message.retain = False
        message.payload = b"not a mode"
        publisher.on_message(client, None, message)
        self.assertIsNone(publisher.take_command())
        message.payload = b"USB"
        clock.return_value = 30
        publisher.on_message(client, None, message)
        self.assertIsNone(publisher.take_command())

    def test_disconnect_clears_commands_and_reconnect_needs_new_settings(self):
        publisher = Publisher(ADDRESS, allow_control=True)
        client = MagicMock()
        client.is_connected.return_value = True
        publisher.client = client
        values = {"output_source_priority": "SBU", "charger_source_priority": "Solar only"}
        publisher.publish_controls(values)
        message = MagicMock(topic=f"{publisher.base}/charger_source_priority/set", payload=b"Solar and utility", retain=False)
        publisher.on_message(client, None, message)
        publisher.on_disconnect(client, None, None, 0, None)
        self.assertIsNone(publisher.take_command())
        publisher.on_connect(client, None, None, 0, None)
        publisher.on_message(client, None, message)
        self.assertIsNone(publisher.take_command())
        publisher.publish_controls(values)
        publisher.on_message(client, None, message)
        self.assertEqual(publisher.take_command(), ("charger_source_priority", "Solar and utility"))

    def test_queued_command_expires_and_does_not_replace_a_pending_command(self):
        clock = MagicMock(return_value=0)
        publisher = Publisher(ADDRESS, allow_control=True, clock=clock, control_fresh_seconds=100)
        publisher.client = MagicMock()
        publisher.client.is_connected.return_value = True
        publisher.publish_controls({"output_source_priority": "SBU", "charger_source_priority": "Solar only"})
        message = MagicMock(topic=f"{publisher.base}/output_source_priority/set", payload=b"USB", retain=False)
        publisher.on_message(publisher.client, None, message)
        message.payload = b"SUB"
        publisher.on_message(publisher.client, None, message)
        self.assertEqual(publisher.take_command(), ("output_source_priority", "USB"))
        publisher.on_message(publisher.client, None, message)
        clock.return_value = 30
        self.assertIsNone(publisher.take_command())

    def test_service_credentials_from_supervisor(self):
        response = MagicMock()
        response.__enter__.return_value = BytesIO(json.dumps({
            "result": "ok", "data": {
                "host": "172.0.0.17", "port": "1883",
                "username": "addons", "password": "secret",
            },
        }).encode())
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "test-token"}), \
             patch("mqtt_bridge.urlopen", return_value=response) as urlopen:
            service = supervisor_service()
        self.assertEqual(service["host"], "172.0.0.17")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")


if __name__ == "__main__":
    unittest.main()
