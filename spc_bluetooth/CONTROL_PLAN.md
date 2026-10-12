# Bluetooth output-source priority control

Status: standalone USB, SBU, and SUB writes were verified on this inverter on 2026-10-12. App 0.2.5 adds output and charger priority selectors when `allow_control: true`. Output writes are hardware tested; charger writes await hardware verification.

## Evidence from this inverter

The saved `laptop-metrics/inventory.json` lists characteristic `00002a0c-0000-1000-8000-00805f9b34fb` under service `00001810-0000-1000-8000-00805f9b34fb` as readable and writable. GATT paths are discovered at connection time; saved paths must not be hard-coded.

Two saved diagnostic packets are 20 bytes long. Both have byte 17 = 2 and byte 18 = 3. The independent reference decoder identifies byte 17 as output-source priority (0 USB, 1 SUB, 2 SBU) and byte 18 as charger-source priority (3 solar-only). The later hardware test confirmed code 0 as USB and code 2 as the previous SBU setting. The SUB test also confirmed code 1 by settings readback and reconnect.

The same readable settings block includes output voltage/frequency, battery charging limits, and battery voltage thresholds. The standalone test established that this firmware accepted a fresh 20-byte block with only byte 17 changed. This finding is specific to the tested inverter/firmware; keep preserving every other byte and checking full readback for each command.

Output-source priority is separate from the live L/B operating-mode sensor. Changing priority does not necessarily force an immediate L/B transition: source availability and battery thresholds still apply.

## Verified standalone test (2026-10-12)

- Initial priority: SBU; charger priority: solar-only.
- One `WriteValue` request to characteristic 2a0c changed only byte 17 from 2 to 0.
- Readback matched the intended 20-byte block exactly; every other byte was preserved.
- A separate Bluetooth reconnection read USB again.
- The user confirmed USB on the inverter display.
- A second user-requested test switched USB back to SBU, with exact readback and a separate reconnect confirming SBU. Solar-only charging and every other byte were preserved.
- A third user-requested test switched SBU to SUB (byte 17 = 1), with exact readback and a separate reconnect confirming SUB. Every other byte, including solar-only charger priority, was preserved.
- The inverter was later switched back to SBU at the user's request (write report `priority-20261012T090930-029406Z.json`). SBU and SUB have been verified through settings reads; only USB was separately confirmed on the physical display.
- Reports are under `priority-test-results/`: `priority-20261012T085921-181860Z.json` (USB write), `priority-20261012T090045-609181Z.json` (USB reconnect), `priority-20261012T090435-960651Z.json` (SBU write), and `priority-20261012T090510-761959Z.json` (SBU reconnect), `priority-20261012T090652-386462Z.json` (SUB write), and `priority-20261012T090724-303395Z.json` (SUB reconnect).

Use an ignored `options.local.json` populated with your connection settings.
From the repository root, with the Home Assistant logger stopped, phone Bluetooth off, and laptop beside the inverter:

```sh
# Read-only preview
PYTHONPATH=.venv python3 spc_bluetooth/priority_test.py --options options.local.json --mode USB
# One explicit write, preserving all other settings from a fresh read
PYTHONPATH=.venv python3 spc_bluetooth/priority_test.py --options options.local.json --mode USB --apply
# Switch to SBU instead
PYTHONPATH=.venv python3 spc_bluetooth/priority_test.py --options options.local.json --mode SBU --apply
# Switch to SUB instead
PYTHONPATH=.venv python3 spc_bluetooth/priority_test.py --options options.local.json --mode SUB --apply
```

`--apply` does not write if the target priority is already selected. It saves the original settings before an attempt, detects changes before writing, and does not retry or automatically roll back a failed/unconfirmed write. The script requires the laptop to already be paired. If a test fails, inspect its report and the inverter display before another attempt.

## Optional Android capture for other firmware or commands

1. Stop SPC Bluetooth Logger in Home Assistant so WatchPower can connect.
2. On the Android phone, enable Developer options, then enable Bluetooth HCI snoop log. If a choice is shown, use full/enabled logging rather than filtered logging.
3. Turn Bluetooth off and on so logging takes effect.
4. Connect WatchPower to this inverter. Note the current output-source priority.
5. In Output settings > Output source priority, select USB and Apply, then SUB and Apply, then SBU and Apply. Wait for each setting to be confirmed. Note the order and times. These changes affect the power source used by the loads. Restore the original priority at the end.
6. In Developer options, take a full bug report and save/share it to the Linux laptop. Extract the ZIP and search for `btsnoop_hci.log` or similarly named Bluetooth snoop files (often under `FS/data/misc/bluetooth/logs`). Send only the Bluetooth snoop file, along with the order of successful changes.
7. Disable HCI logging, disconnect WatchPower, and restart the Home Assistant app.

Some Android builds embed Bluetooth logs in the report text instead of including a separate file. If no snoop file is present, the Android phone model/version will determine the extraction step; Android's `btsnooz.py` can extract embedded snoop data locally.

## Implemented Home Assistant behavior (0.2.5)

- Create an MQTT Select named Output source priority with USB, SUB, and SBU options. Report the inverter's read-back priority rather than the requested value.
- Subscribe to a command topic without retaining commands. Reject invalid or retained commands and discard pending commands on Bluetooth disconnect; reconnecting must not replay a mode change.
- Execute commands in the existing Bluetooth connection alongside polling, with one operation at a time.
- Use the locally verified settings-block write format. Charger priority maps byte 18 (3 solar-only, 2 solar and utility) using the reference decoder; output-priority changes preserve it. Preserve charger priority (solar-only) and all unrelated settings, using a fresh settings read whenever the command format requires a whole settings block.
- Read back after the write; confirm the selected value and the preserved settings before reporting success. A missing response or mismatched readback is a failed/unconfirmed operation.
- Controls require a working Bluetooth connection and fresh settings, even when measurement sensors are displaying held values during the 0.2.4 grace period.
- Add the selector to the inverter dashboard once the command and readback have been verified on hardware.

References:

- WatchPower Bluetooth app manual: https://www.mppsolar.com/manual/Watchpower%20(off-grid)/Watchpower_APP%20android_manual.pdf
- Reference read decoder: https://github.com/darkbox/bluetooth_inverter_power_watch/blob/master/bt/src/inverter/mod.rs
- Android Bluetooth capture instructions: https://source.android.com/docs/core/connect/bluetooth/verifying_debugging
- Home Assistant MQTT Select: https://www.home-assistant.io/integrations/select.mqtt/
