# 0.2.6

- Remove bundled Bluetooth addresses and PINs; read them from configuration.
- Use the Home Assistant Configuration tab or standalone --options JSON files.
- Add a blank options.example.json; ignore local settings and device captures.
- Replace personal connection identifiers in tests with synthetic fixtures.

# 0.2.5

- Add optional MQTT dropdowns for output priority (USB/SUB/SBU) and battery charging source (Solar only/Solar and utility).
- Read settings before every command and change only the requested priority byte.
- Confirm full settings readback before publishing the new dropdown value.
- Disable controls and clear pending commands on Bluetooth/MQTT disconnect; reject retained, invalid, and stale commands.
- Save the latest command's original and resulting settings in last-command.json.
- Keep measurement sensors' configurable disconnect grace period.
- Output priority verified on this inverter for USB, SUB, and SBU. Charger writes use the reference mapping and await hardware testing.

# 0.2.4

- Keep each Home Assistant sensor's last reading during brief Bluetooth failures.
- Add configurable `stale_seconds` timeout (default 180 seconds).
- Expire sensors independently from the last real reading, even during blocked Bluetooth operations.
- Preserve recent availability on MQTT reconnects; never-read fields remain unavailable.
- Keep raw logs unchanged so failed reads remain visible.

# 0.2.3

- Allow AppArmor to read `/app/` directory metadata and contents so Python can locate modules.
- Keep startup diagnostics running even if directory listing is denied.

# 0.2.2

- Print installed app version, files, and Python import path at startup to diagnose the missing decoder module.
- Force a new image build with the module check retained.

# 0.2.1

- Validate all Python modules during image build; fail the build if the upload is incomplete.

# 0.2.0

- MQTT discovery creates Home Assistant sensors for all 15 decoded values.
- Sensor availability tracks missing readings and Bluetooth/MQTT disconnects.
- Solar service mapping confirmed from a live SPC III capture.

# 0.1.0

- Read-only Bluetooth polling, PIN pairing, reconnection, and raw diagnostics.
- Candidate decoder for AC, battery, PV, operating mode, and raw event flags.
- Daily CSV and JSONL files with retention and a browser download page.
