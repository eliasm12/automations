"""Publish decoded readings as Home Assistant MQTT discovery sensors."""
import json
import logging
import os
import ssl
import time
from collections import deque
from threading import Lock

from controls import SELECTS
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

LOG = logging.getLogger("spc.mqtt")

# Display name, unit, Home Assistant device class. The decoded fields are in
# decoder.METRICS; adding a metric there requires adding its metadata here.
SENSORS = {
    "grid_voltage_v": ("Grid voltage", "V", "voltage"),
    "grid_frequency_hz": ("Grid frequency", "Hz", "frequency"),
    "output_voltage_v": ("Output voltage", "V", "voltage"),
    "output_frequency_hz": ("Output frequency", "Hz", "frequency"),
    "output_apparent_power_va": ("Output apparent power", "VA", None),
    "output_power_w": ("Output power", "W", "power"),
    "load_percent": ("Load", "%", None),
    "battery_voltage_v": ("Battery voltage", "V", "voltage"),
    "battery_charge_current_a": ("Battery charge current", "A", "current"),
    "battery_capacity_percent": ("Battery capacity", "%", None),
    "battery_discharge_current_a": ("Battery discharge current", "A", "current"),
    "operating_mode_code": ("Operating mode code", None, None),
    "event_flags_hex": ("Event flags", None, None),
    "pv_voltage_v": ("PV voltage", "V", "voltage"),
    "pv_power_w": ("PV power", "W", "power"),
}


def supervisor_service():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN is missing")
    request = Request("http://supervisor/services/mqtt",
                      headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
    if payload.get("result") != "ok" or not isinstance(payload.get("data"), dict):
        raise RuntimeError("MQTT service is unavailable")
    return payload["data"]


class Publisher:
    def __init__(self, address, service_provider=supervisor_service,
                 stale_seconds=180, clock=time.monotonic, allow_control=False,
                 control_fresh_seconds=30):
        self.id = address.lower().replace(":", "")
        self.base = f"spc_bluetooth/{self.id}"
        self.bridge_topic = f"{self.base}/bridge"
        self.service_provider = service_provider
        self.client = None
        self.stale_seconds = stale_seconds
        self.clock = clock
        self.last_seen = {}
        self.allow_control = allow_control
        self.control_fresh_seconds = control_fresh_seconds
        self.control_topic = f"{self.base}/controls/available"
        self.control_lock = Lock()
        self.control_seen = None
        self.commands = deque()

    def config(self, key):
        name, unit, device_class = SENSORS[key]
        item = {
            "name": name,
            "unique_id": f"spc_{self.id}_{key}",
            "state_topic": f"{self.base}/{key}/state",
            # HA expires independently even if Bluetooth operations are blocked.
            "expire_after": self.stale_seconds,
            "availability": [
                {"topic": self.bridge_topic},
                {"topic": f"{self.base}/{key}/available"},
            ],
            "availability_mode": "all",
            "device": {
                "identifiers": [f"spc_{self.id}"],
                "name": "Alpha OutBack SPC III 5000-48",
                "manufacturer": "Alpha OutBack",
                "model": "SPC III 5000-48",
            },
        }
        if unit:
            item["unit_of_measurement"] = unit
            item["state_class"] = "measurement"
        if device_class:
            item["device_class"] = device_class
        return item

    def select_config(self, key):
        return {
            "name": SELECTS[key][0],
            "unique_id": f"spc_{self.id}_{key}",
            "default_entity_id": f"select.alpha_outback_spc_iii_5000_48_{key}",
            "state_topic": f"{self.base}/{key}/state",
            "command_topic": f"{self.base}/{key}/set",
            "options": list(SELECTS[key][2]),
            "optimistic": False,
            "retain": False,
            "qos": 0,
            "entity_category": "config",
            "availability": [{"topic": self.bridge_topic}, {"topic": self.control_topic}],
            "availability_mode": "all",
            "device": self.config("pv_power_w")["device"],
        }

    def disable_controls(self):
        with self.control_lock:
            self.control_seen = None
            self.commands.clear()
        if self.allow_control and self.client is not None and self.client.is_connected():
            self.client.publish(self.control_topic, "offline", qos=1, retain=True)

    def publish_controls(self, values):
        if not self.allow_control or self.client is None or not self.client.is_connected():
            self.disable_controls()
            return
        for key in SELECTS:
            value = values[key]
            # A recognized but unexposed setting must not masquerade as another choice.
            state = value if value in SELECTS[key][2] else "None"
            self.client.publish(f"{self.base}/{key}/state", state, qos=1, retain=True)
        with self.control_lock:
            self.control_seen = self.clock()
        self.client.publish(self.control_topic, "online", qos=1, retain=True)

    def take_command(self):
        with self.control_lock:
            if not self.commands:
                return None
            command = self.commands.popleft()
            if (self.control_seen is None or self.clock() - self.control_seen >= self.control_fresh_seconds
                    or self.clock() - command[2] >= 30):
                self.commands.clear()
                return None
            return command[:2]

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.disable_controls()

    def discovery(self, client):
        for key in SENSORS:
            topic = f"homeassistant/sensor/spc_{self.id}/{key}/config"
            client.publish(topic, json.dumps(self.config(key)), qos=1, retain=True)
        for key in SELECTS:
            topic = f"homeassistant/select/spc_{self.id}/{key}/config"
            # Remove earlier dropdowns if controls have been disabled in options.
            payload = json.dumps(self.select_config(key)) if self.allow_control else ""
            client.publish(topic, payload, qos=1, retain=True)

    def is_fresh(self, key, now):
        seen = self.last_seen.get(key)
        return seen is not None and now - seen < self.stale_seconds

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        LOG.info("MQTT connected; advertising %d sensors", len(SENSORS))
        self.disable_controls()
        self.discovery(client)
        client.publish(self.bridge_topic, "online", qos=1, retain=True)
        # Keep recent readings across broker reconnects within this process.
        # A newly started process has no readings and starts offline.
        now = self.clock()
        for key in SENSORS:
            availability = "online" if self.is_fresh(key, now) else "offline"
            client.publish(f"{self.base}/{key}/available", availability, qos=1, retain=True)
        client.subscribe("homeassistant/status", qos=0)
        if self.allow_control:
            for key in SELECTS:
                client.subscribe(f"{self.base}/{key}/set", qos=0)

    def on_message(self, client, userdata, message):
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self.discovery(client)
            self.disable_controls()
            return
        if not self.allow_control or message.retain:
            return
        key = next((k for k in SELECTS if message.topic == f"{self.base}/{k}/set"), None)
        if key is None:
            return
        try:
            option = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        with self.control_lock:
            if (option not in SELECTS[key][2] or self.control_seen is None
                    or self.clock() - self.control_seen >= self.control_fresh_seconds):
                LOG.warning("Rejected stale/unavailable/invalid priority command: %s", key)
                return
            if self.commands:
                LOG.warning("Rejected priority command while another is pending")
                return
            self.commands.append((key, option, self.clock()))

    def start(self):
        if self.client is not None:
            return
        service = self.service_provider()
        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                             client_id=f"spc-{self.id}")
        client.username_pw_set(service["username"], service["password"])
        if service.get("ssl"):
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.will_set(self.bridge_topic, "offline", qos=1, retain=True)
        client.on_connect = self.on_connect
        client.on_message = self.on_message
        client.on_disconnect = self.on_disconnect
        client.connect_async(service["host"], int(service["port"]), keepalive=60)
        client.loop_start()
        self.client = client

    def publish_sample(self, result):
        try:
            self.start()
        except (OSError, RuntimeError, KeyError, ValueError) as error:
            LOG.warning("MQTT not ready: %s", error)
            return
        if not self.client.is_connected():
            return
        values = result["metrics"]
        now = self.clock()
        for key in SENSORS:
            if key in values:
                self.last_seen[key] = now
                self.client.publish(f"{self.base}/{key}/state", str(values[key]), qos=0)
                self.client.publish(f"{self.base}/{key}/available", "online", qos=1, retain=True)
            elif not self.is_fresh(key, now):
                self.client.publish(f"{self.base}/{key}/available", "offline", qos=1, retain=True)
            # Within the grace period, send neither a cached state nor offline.
            # Replaying a cached state would incorrectly restart HA's expiry.

    def stop(self):
        self.disable_controls()
        if self.client is None:
            return
        if self.client.is_connected():
            message = self.client.publish(self.bridge_topic, "offline", qos=1, retain=True)
            message.wait_for_publish(timeout=3)
        self.client.disconnect()
        self.client.loop_stop()
