# SPC III Bluetooth logger

A small Python poller and Home Assistant OS add-on for the Alpha OutBack
SPC III 5000-48. Connection settings are supplied through configuration; no
Bluetooth device address or PIN is bundled in the code.

It uses Linux BlueZ through the general-purpose `dbus-fast` package, with our
own measurement decoder. No inverter library, Bleak, or cloud account.
The Home Assistant add-on publishes MQTT discovery sensors through the
Supervisor-provided broker service. Brief Bluetooth failures keep the last
Home Assistant sensor readings for a configurable grace period (`stale_seconds`,
default 180 seconds); longer outages expire them. Version 0.2.5 adds optional
Home Assistant dropdowns for output and charging priorities (`allow_control: true`).
They use fresh settings reads and confirmed readback; command details are saved
in `last-command.json`.

**Live tested on this inverter from the laptop (2026-09-25).** Pairing,
read-only GATT polling, PV/load/battery decoding, and repeated CSV logging
worked. Home Assistant OS installation and MQTT discovery have not yet been
tested on the Pi; compare the numbers with WatchPower before treating them as
calibrated measurements.

See [installation and operation](spc_bluetooth/DOCS.md).

## Run on the laptop

This workspace has the Bluetooth dependency staged in `.venv` for the current
laptop. Copy `options.example.json` to `options.local.json` and fill in your
Bluetooth address and PIN. The local file is ignored by Git. Home Assistant
uses its app Configuration tab instead. Put the laptop near the inverter,
disconnect WatchPower, then run:

```sh
PYTHONPATH=.venv python3 spc_bluetooth/poller.py --options options.local.json --output-dir ./laptop-metrics
```

Press Ctrl+C to stop. Logs are written under `laptop-metrics/`. Add
`--diagnostics` to save all readable raw characteristics; add `--once` for
one connection/read attempt. On another Linux computer, install Python venv
support and follow the standalone setup in the add-on documentation.

## Local checks

```sh
python3 -m venv .venv
.venv/bin/pip install -r spc_bluetooth/requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Protocol and platform references

- [WatchPower-style characteristic layout](https://github.com/darkbox/bluetooth_inverter_power_watch/blob/master/bt/src/inverter/mod.rs)
- [BlueZ GATT API](https://bluez.readthedocs.io/en/latest/gatt-api/)
- [BlueZ pairing agent API](https://bluez.readthedocs.io/en/latest/agent-api/)
- [Home Assistant add-on configuration](https://developers.home-assistant.io/docs/apps/configuration/)

The implementation uses the numeric field layout; it does not bundle the
reference project's source. Battery-management-system data is outside this logger's scope; the app can
change output and charging priorities when controls are enabled.
