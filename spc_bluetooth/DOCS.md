# SPC Bluetooth Logger

## Install on Home Assistant OS

This is a **local add-on**, not YAML for `configuration.yaml` or an automation.
Files must first be copied into HA's `/addons` directory. SSH is not required.

1. In **Settings → Apps**, install and start **Mosquitto broker**. Then in
   **Settings → Devices & services**, add the discovered **MQTT** integration
   and keep MQTT discovery enabled. Existing MQTT brokers also work if they
   provide Home Assistant’s MQTT service.
2. Extract the supplied archive on your computer. It contains `spc_bluetooth`.
3. In Home Assistant install and configure **Samba share**. Enable its `local_apps` share (`addons` on older versions)
   if it is not already enabled. Consult its Documentation tab for the
   options in your installed version.
4. Open the network share `\\192.168.102.2\local_apps` (Windows), or
   `smb://192.168.102.2/local_apps` (macOS/Linux), using your Samba credentials.
5. Copy the entire `spc_bluetooth` folder there. The resulting path must be
   `/addons/spc_bluetooth/config.yaml`, with no extra nested folder.
6. Open **Settings → Add-ons → Add-on Store** (newer versions call these
   **Apps**). Use the store menu to **Check for updates**, or reload the store.
   Under **Local add-ons**, open **SPC Bluetooth Logger → Install**.
   The first build needs internet access for the base image and dependency.
7. Enter your inverter Bluetooth `address` and six-digit `pin` in the app
   Configuration tab; neither has a bundled default. Keep `hci0` and leave
   `mqtt: true` to create sensors. Existing saved settings are used on upgrade.
   Put the Pi nearby and turn off the phone's Bluetooth.
8. Start the add-on and inspect **Logs**. Click **Open Web UI** to view readings
   and download CSV/raw logs. Under **Settings → Devices & services → MQTT**,
   open **Alpha OutBack SPC III 5000-48** to see its 15 sensor entities.
   Enable **Start on boot** after a successful test.

Supported builds: `aarch64` (64-bit Raspberry Pi OS image) and `amd64`.
Pi 3 hardware can run aarch64, but an old 32-bit HA OS installation will not
match this manifest. Check System information before installing; do not
reinstall the OS just to test the logger.

No host port is published. The page uses Home Assistant ingress. The add-on
reads broker connection details from the Supervisor MQTT service. It does not
store broker passwords in its own options. The broker must be installed and
MQTT discovery enabled for Home Assistant sensor entities to appear.
The add-on uses the existing host BlueZ service. Its pairing agent handles only
this inverter; it does not replace HA's default agent or restart the adapter.
Keep Protection mode on.

## Files and measurements

Saved in `/share/spc_bluetooth`, also available through the browser page:

- `metrics-YYYY-MM-DD.csv`: UTC timestamps and metrics, one row per attempt.
- `raw-YYYY-MM-DD.jsonl`: raw bytes, errors, and decoded metrics per attempt.
- `latest.json`: latest attempt, atomically replaced; failures have empty metrics.
- `inventory.json`: last successful discovery of services and characteristics.
- `last-command.json`: latest control attempt with before/planned/after settings; created only after a command.

Daily files are retained for 14 UTC calendar days by default. Adjust
`retention_days` (1–365). Cleanup removes only this logger's dated CSV/JSONL
files. Download backups if you want longer history.

Normal polling reads `2a03` (AC/battery), `2a04` (battery/status), and `2a11`
(first PV channel). AC and battery are in service `1810`; PV is in service `1811`. Each measurement read uses GATT `ReadValue`, not cached advertisements. With `allow_control: false`, there are no inverter characteristic writes. Enabling controls adds settings reads and user-requested priority writes to characteristic `2a0c`.
Pairing does save a Bluetooth bond in the host's BlueZ storage.

Missing readings stay blank, not zero. `partial` means some reads failed;
`unavailable` means no usable metrics. Errors trigger reconnection after the
polling delay. Characteristics are read sequentially, not as an atomic snapshot.

## First hardware validation

1. Set `diagnostics: true` to read all readable characteristics in services
   `1810` and `1811`. Download `inventory.json`, `latest.json`, and the raw log.
2. Compare battery voltage, load watts, and PV watts with the inverter display.
   To compare with WatchPower, stop the add-on before reconnecting your phone.
3. The decoder has been exercised with live data on a laptop, but check the
   values against your display or WatchPower. Values are provisional until
   confirmed. `protocol_validated: false` is intentional. `status: ok` means reads and structural decoding succeeded,
   not that compatibility has been proven.
4. If the layout differs, use raw bytes to adjust the decoder. Short packets
   and sentinel values are rejected instead of padded. Battery capacity is the
   inverter's estimate, not an independently measured state of charge.
5. Once confirmed, set `diagnostics: false` and test restarts/reconnection.

With `mqtt: true`, MQTT discovery creates 15 Home Assistant sensor entities
for the decoded fields. Brief missing reads or Bluetooth disconnects keep the
last successful value for up to `stale_seconds` (default 180 seconds). Each
field has its own timeout, counted from its last actual reading. Repeated
failures do not extend it. Never-read fields start unavailable; fresh readings
restore availability automatically. Set `stale_seconds` in the app Configuration
tab (5–86400 seconds); choose a value comfortably above `poll_seconds`.
MQTT discovery uses Home Assistant's `expire_after` timer so readings expire
even if the poller is waiting for pairing or service discovery. Stopping the app
or losing the MQTT connection still makes sensors unavailable immediately.
Raw logs and the Web UI report failed attempts immediately and do not replace
missing data with cached values. Energy helpers may integrate the held power
during a short outage, assuming the load has not changed.
These are live measurements and status fields,
not cumulative kWh totals. The Energy dashboard needs validated energy totals
or integration helpers; do not add power values as if they were energy.

## Change priorities from Home Assistant (0.2.5 and later)

After copying the updated folder to the Samba `local_apps`/`addons` share, check for updates in the app store and update SPC Bluetooth Logger to 0.2.6 or later. In the app Configuration tab, keep `mqtt: true` and set:

```yaml
allow_control: true
```

Start the app with WatchPower disconnected. MQTT discovery adds two dropdowns to **Settings > Devices & services > MQTT > Alpha OutBack SPC III 5000-48**:

- **Output source priority:** USB, SUB, SBU.
- **Battery charging source:** Solar only, Solar and utility.

Both show settings read from the inverter. Startup and reconnect only read settings; they do not apply a default priority. Output-priority writes have been verified on this inverter for all three choices. Charger priority uses the reference protocol mapping (byte 18: 3 = solar-only; 2 = solar and utility); a charger change has not yet been exercised on this hardware. Each requested change requires exact readback before its dropdown updates.

A command reads the current settings, changes one priority byte, and preserves all other bytes, including current limits and battery voltage thresholds. Selecting Solar and utility allows utility charging alongside solar where the inverter's operating mode permits it; it does not override existing charging-current limits or force charging while the battery is full. Do not interpret changing the charging source as changing USB/SUB/SBU output priority.

Controls become unavailable immediately when the app detects Bluetooth disconnection. Measurement sensors still keep their 180-second grace period. Commands are serialized, accepted only with a recent settings read, and never queued across a disconnect or replayed after restart. Failed or timed-out writes are not retried automatically. The latest command's original, planned, and read-back settings are saved as `/share/spc_bluetooth/last-command.json`, downloadable in the Web UI. Inspect the report and inverter display if a write is unconfirmed.

For a dashboard dropdown card, paste the repository's `spc_controls_card.yaml` into **Add card > Manual**, or re-paste the updated full `spc_dashboard.yaml`. The expected entity IDs are `select.alpha_outback_spc_iii_5000_48_output_source_priority` and `select.alpha_outback_spc_iii_5000_48_charger_source_priority`. Check the actual IDs on the MQTT device page and replace them if different.

Setting `allow_control: false` removes the dropdowns and returns the app to monitoring. The inverter keeps the last setting you selected.

## Troubleshooting

- **Not found / timeout:** close WatchPower, turn off phone Bluetooth, move the
  Pi closer. Check that HA's Bluetooth adapter is enabled.
- **Authentication rejected:** verify the configured PIN. Stop this add-on before
  trying WatchPower; do not blindly remove existing bonds.
- **D-Bus access denied:** save the logs. Check that `host_dbus` and the supplied
  AppArmor profile are installed; do not disable protection as a first step.
- **Missing characteristics, short packets, unrealistic values:** share the
  inventory and raw output; the protocol needs hardware confirmation.
- **WatchPower cannot connect:** stop the add-on to release the connection.

## Standalone Linux use

Python 3.11+, BlueZ, an enabled adapter, and system D-Bus permissions are needed.
On Home Assistant OS use the add-on instead of these ordinary Linux commands.

```sh
python3 -m venv .venv
.venv/bin/pip install -r spc_bluetooth/requirements.txt
.venv/bin/python spc_bluetooth/poller.py --options options.local.json --once --diagnostics --output-dir ./metrics
```

Omit `--once` for continuous polling. Use `--options /path/to/options.json`
for connection settings. Copy the repository's `options.example.json` to
`options.local.json`, fill in `address` and `pin`, and keep the local file out
of source control. The example deliberately leaves them blank:

```json
{
  "address": "",
  "adapter": "hci0",
  "pin": "",
  "poll_seconds": 20,
  "retention_days": 14,
  "diagnostics": true
}
```

The browser server and MQTT publishing are off by default outside HA. Enabling `web` manually
opens port 8099 with no independent authentication; the add-on instead relies
on Home Assistant's authenticated ingress.
