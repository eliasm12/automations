#!/usr/bin/env python3
"""BlueZ inverter poller with optional Home Assistant priority controls."""

import argparse
import asyncio
from contextlib import suppress
import csv
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import signal

from dbus_fast import BusType, DBusError, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method

from decoder import CHAR_SERVICES, FIELDS, METRICS, PV_SERVICE, SERVICE, decode

LOG = logging.getLogger("spc")
DEVICE = "org.bluez.Device1"
ADAPTER = "org.bluez.Adapter1"
CHAR = "org.bluez.GattCharacteristic1"
GATT_SERVICE = "org.bluez.GattService1"
AGENT_PATH = "/spc/pairing_agent"


class PairingAgent(ServiceInterface):
    """Handle only this inverter's PIN; never replace HA's default agent."""

    def __init__(self, target, pin):
        super().__init__("org.bluez.Agent1")
        self.target, self.pin = target, pin

    def check(self, device):
        if device != self.target:
            raise DBusError("org.bluez.Error.Rejected", "Not the configured inverter")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        self.check(device)
        return self.pin

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        self.check(device)
        return int(self.pin)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        self.check(device)
        if passkey != int(self.pin):
            raise DBusError("org.bluez.Error.Rejected", "Unexpected confirmation code")

    @method()
    def RequestAuthorization(self, device: "o"):
        self.check(device)

    @method()
    def AuthorizeService(self, device: "o", service: "s"):
        self.check(device)
        if service not in (SERVICE, PV_SERVICE):
            raise DBusError("org.bluez.Error.Rejected", "Unexpected service")

    @method()
    def Cancel(self):
        pass

    @method()
    def Release(self):
        pass


async def call(bus, path, interface, member, signature="", body=None, timeout=15):
    response = await asyncio.wait_for(bus.call(Message(
        destination="org.bluez", path=path, interface=interface,
        member=member, signature=signature, body=body or [],
    )), timeout)
    if response is None:
        raise RuntimeError(f"No reply to {member}")
    if response.message_type == MessageType.ERROR:
        raise DBusError(response.error_name, str(response.body))
    return response.body


async def objects(bus):
    return (await call(bus, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]


def prop(interfaces, interface, key, default=None):
    value = interfaces.get(interface, {}).get(key)
    return default if value is None else value.value


async def discover(bus, adapter, target):
    tree = await objects(bus)
    if adapter not in tree:
        raise RuntimeError(f"Bluetooth adapter {adapter} is missing")
    if not prop(tree[adapter], ADAPTER, "Powered", False):
        raise RuntimeError("Bluetooth adapter is powered off; enable it in Home Assistant")
    if target in tree:
        return
    await call(bus, adapter, ADAPTER, "StartDiscovery")
    try:
        for _ in range(30):
            if target in await objects(bus):
                return
            await asyncio.sleep(1)
        raise RuntimeError("Inverter not found; move Pi closer and disconnect WatchPower")
    finally:
        # Release only our discovery session, not Home Assistant's scan.
        with suppress(Exception):
            await call(bus, adapter, ADAPTER, "StopDiscovery")


def inventory(tree, target):
    rows = []
    for path, interfaces in tree.items():
        if not path.startswith(target + "/") or CHAR not in interfaces:
            continue
        service_path = prop(interfaces, CHAR, "Service")
        rows.append({
            "path": path,
            "uuid": prop(interfaces, CHAR, "UUID"),
            "service": prop(tree.get(service_path, {}), GATT_SERVICE, "UUID"),
            "flags": prop(interfaces, CHAR, "Flags", []),
        })
    return sorted(rows, key=lambda item: item["path"])


async def sample(bus, rows, diagnostics):
    raw, metrics, errors = {}, {}, []
    selected = [row for row in rows if "read" in row["flags"] and
                ((diagnostics and row["service"] in (SERVICE, PV_SERVICE)) or
                 row["service"] == CHAR_SERVICES.get(row["uuid"]))]
    if not selected:
        errors.append("No readable inverter characteristics in services 1810/1811")
    counts = {}
    for row in selected:
        counts[row["uuid"]] = counts.get(row["uuid"], 0) + 1
    for row in selected:
        key = row["uuid"]
        try:
            response = await call(bus, row["path"], CHAR, "ReadValue", "a{sv}", [{}])
            payload = bytes(response[0])
            raw[row["path"]] = {"uuid": key, "hex": payload.hex()}
            if key in FIELDS:
                if counts[key] != 1:
                    raise ValueError(f"Ambiguous duplicate characteristic {key}")
                metrics.update(decode(key, payload))
        except (DBusError, TimeoutError, ValueError) as error:
            errors.append(f"{key}: {error}")
    missing = set(FIELDS) - set(counts)
    errors.extend(f"Missing readable characteristic {key}" for key in sorted(missing))
    status = "ok" if metrics and not errors else "partial" if metrics else "unavailable"
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "status": status,
            "protocol_validated": False, "metrics": metrics, "raw": raw, "errors": errors}


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Storage:
    def __init__(self, directory, retention_days):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.last_cleanup = None

    def save(self, result):
        day = result["timestamp"][:10]
        csv_path = self.directory / f"metrics-{day}.csv"
        header = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=["timestamp", "status", *METRICS])
            if header:
                writer.writeheader()
            writer.writerow({"timestamp": result["timestamp"], "status": result["status"],
                             **result["metrics"]})
        with (self.directory / f"raw-{day}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result) + "\n")
        atomic_json(self.directory / "latest.json", result)
        if self.last_cleanup != day:
            cutoff = (datetime.fromisoformat(day) - timedelta(days=self.retention_days - 1)).date()
            for path in self.directory.iterdir():
                match = re.fullmatch(r"(?:metrics-(\d{4}-\d{2}-\d{2})\.csv|raw-(\d{4}-\d{2}-\d{2})\.jsonl)", path.name)
                if match:
                    with suppress(ValueError):
                        if datetime.fromisoformat(match[1] or match[2]).date() < cutoff:
                            path.unlink()
            self.last_cleanup = day


async def wait_for_poll(seconds, controller=None, publisher=None):
    if controller is None:
        await asyncio.sleep(seconds)
        return
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        command = publisher.take_command()
        if command is not None:
            await controller.execute(*command)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(1, remaining))


async def connection(options, storage, publisher=None):
    adapter = f"/org/bluez/{options['adapter']}"
    target = adapter + "/dev_" + options["address"].replace(":", "_")
    bus = MessageBus(bus_type=BusType.SYSTEM)
    connected = False
    registered = False
    controller = None
    if publisher:
        publisher.disable_controls()
    try:
        await asyncio.wait_for(bus.connect(), 15)
        await discover(bus, adapter, target)
        bus.export(AGENT_PATH, PairingAgent(target, options["pin"]))
        await call(bus, "/org/bluez", "org.bluez.AgentManager1", "RegisterAgent",
                   "os", [AGENT_PATH, "KeyboardOnly"])
        registered = True
        tree = await objects(bus)
        if options["pair"] and not prop(tree.get(target, {}), DEVICE, "Paired", False):
            LOG.info("Pairing with %s", options["address"])
            try:
                await call(bus, target, DEVICE, "Pair", timeout=60)
            except BaseException:
                with suppress(Exception):
                    await call(bus, target, DEVICE, "CancelPairing", timeout=5)
                raise
        LOG.info("Connecting to %s", options["address"])
        # Also clean up a connection that times out after BlueZ has started it.
        connected = True
        await call(bus, target, DEVICE, "Connect", timeout=30)
        for _ in range(30):
            tree = await objects(bus)
            if prop(tree.get(target, {}), DEVICE, "ServicesResolved", False):
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Bluetooth service discovery timed out")
        rows = inventory(tree, target)
        atomic_json(storage.directory / "inventory.json", rows)
        LOG.info("Connected; discovered %d characteristics", len(rows))
        if options.get("allow_control", False) and publisher:
            from controls import ControlSession, settings_path
            controller = ControlSession(bus, settings_path(rows), call, publisher,
                lambda report: atomic_json(storage.directory / "last-command.json", report))
        while True:
            tree = await objects(bus)
            if not prop(tree.get(target, {}), DEVICE, "Connected", False):
                raise RuntimeError("Inverter disconnected")
            result = await sample(bus, rows, options["diagnostics"])
            storage.save(result)
            if publisher:
                publisher.publish_sample(result)
            LOG.info("%s %s", result["status"], json.dumps(result["metrics"]))
            for error in result["errors"]:
                LOG.warning("%s", error)
            if controller and not result["errors"]:
                # A fresh read publishes the actual dropdown values; startup never writes.
                await controller.refresh()
            if options["once"]:
                return result["status"] != "unavailable"
            if result["errors"]:
                raise RuntimeError("Incomplete read; reconnecting before next poll")
            await wait_for_poll(options["poll_seconds"], controller, publisher)
    finally:
        if publisher:
            publisher.disable_controls()
        if connected:
            with suppress(Exception):
                await call(bus, target, DEVICE, "Disconnect", timeout=5)
        if registered:
            with suppress(Exception):
                await call(bus, "/org/bluez", "org.bluez.AgentManager1", "UnregisterAgent",
                           "o", [AGENT_PATH], timeout=5)
        bus.disconnect()


async def run(options, storage, publisher=None):
    while True:
        try:
            return 0 if await connection(options, storage, publisher) else 1
        except (DBusError, TimeoutError, RuntimeError, ValueError, OSError, EOFError) as error:
            LOG.error("%s: %s", type(error).__name__, error)
            unavailable = {"timestamp": datetime.now(timezone.utc).isoformat(),
                           "status": "unavailable", "protocol_validated": False,
                           "metrics": {}, "raw": {}, "errors": [str(error)]}
            storage.save(unavailable)
            if publisher:
                publisher.publish_sample(unavailable)
            if options["once"]:
                return 1
            await asyncio.sleep(options["poll_seconds"])


def settings():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--options", type=Path, help="Home Assistant options.json or standalone JSON")
    parser.add_argument("--once", action="store_true", help="One connection/read attempt, then exit")
    parser.add_argument("--diagnostics", action="store_true", help="Read all readable 1810/1811 characteristics")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    options = {"address": "", "adapter": "hci0", "pin": "",
               "pair": True, "poll_seconds": 20, "stale_seconds": 180, "retention_days": 14,
               "diagnostics": False, "once": False, "web": False, "mqtt": False,
               "allow_control": False,
               "output_dir": "./metrics"}
    if args.options:
        options.update(json.loads(args.options.read_text(encoding="utf-8")))
    options["once"] = args.once or options["once"]
    options["diagnostics"] = args.diagnostics or options["diagnostics"]
    if args.output_dir:
        options["output_dir"] = str(args.output_dir)
    if not isinstance(options["address"], str) or not options["address"]:
        parser.error("Set address in the app Configuration tab or your --options JSON file")
    options["address"] = options["address"].upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", options["address"]):
        parser.error("Invalid Bluetooth address")
    if not re.fullmatch(r"hci\d+", options["adapter"]):
        parser.error("Adapter must be hci0, hci1, etc.")
    if not isinstance(options["pin"], str) or not re.fullmatch(r"[0-9]{6}", options["pin"]):
        parser.error("Set pin to a quoted six-digit string in your configuration")
    for key in ("pair", "diagnostics", "once", "web", "mqtt", "allow_control"):
        if type(options[key]) is not bool:
            parser.error(f"{key} must be a boolean")
    for key, minimum, maximum in (("poll_seconds", 5, 3600), ("stale_seconds", 5, 86400),
                                ("retention_days", 1, 365)):
        if type(options[key]) is not int or not minimum <= options[key] <= maximum:
            parser.error(f"{key} must be an integer from {minimum} to {maximum}")
    if options["allow_control"] and not options["mqtt"]:
        parser.error("allow_control requires mqtt: true")
    return options


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    options = settings()
    storage = Storage(options["output_dir"], options["retention_days"])
    atomic_json(storage.directory / "latest.json", {"status": "starting", "metrics": {},
                "timestamp": datetime.now(timezone.utc).isoformat(), "protocol_validated": False})
    publisher = None
    if options["mqtt"]:
        from mqtt_bridge import Publisher
        publisher = Publisher(options["address"], stale_seconds=options["stale_seconds"],
                              allow_control=options["allow_control"],
                              control_fresh_seconds=options["poll_seconds"] + 60)
    server = None
    if options["web"]:
        from web import start_server
        server = start_server(storage.directory)
    async def managed():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            return await run(options, storage, publisher)
        except asyncio.CancelledError:
            return 0
        finally:
            if publisher:
                publisher.stop()
            if server:
                server.shutdown()
    return asyncio.run(managed())


if __name__ == "__main__":
    raise SystemExit(main())
