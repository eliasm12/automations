"""Candidate SPC III/WatchPower layout; validate against the actual inverter.

Protocol reference (independent implementation):
https://github.com/darkbox/bluetooth_inverter_power_watch/blob/master/bt/src/inverter/mod.rs
Only expected characteristics under the inverter's 1810/1811 services are eligible.
"""

import struct


def uuid(short):
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


SERVICE = uuid("1810")
PV_SERVICE = uuid("1811")
FIELDS = {
    uuid("2a03"): (
        20,
        (("grid_voltage_v", 0, 10), ("grid_frequency_hz", 2, 10),
         ("output_voltage_v", 4, 10), ("output_frequency_hz", 6, 10),
         ("output_apparent_power_va", 8, 1), ("output_power_w", 10, 1),
         ("load_percent", 12, 1), ("battery_voltage_v", 16, 100),
         ("battery_charge_current_a", 18, 1)),
    ),
    uuid("2a04"): (
        13,
        (("battery_capacity_percent", 0, 1), ("battery_discharge_current_a", 4, 1)),
    ),
    uuid("2a11"): (16, (("pv_voltage_v", 12, 10), ("pv_power_w", 14, 1))),
}
CHAR_SERVICES = {key: PV_SERVICE if key == uuid("2a11") else SERVICE for key in FIELDS}
METRICS = [name for _, fields in FIELDS.values() for name, _, _ in fields]
METRICS += ["operating_mode_code", "event_flags_hex"]


def decode(characteristic, payload):
    """Reject short packets; never pad missing values or reuse older readings."""
    minimum, fields = FIELDS[characteristic]
    if len(payload) < minimum:
        raise ValueError(f"{characteristic}: expected >= {minimum} bytes, got {len(payload)}")
    values = {}
    for name, offset, divisor in fields:
        number = struct.unpack_from("<H", payload, offset)[0]
        if number == 65535:
            raise ValueError(f"{name}: unsupported/sentinel value 65535")
        values[name] = number if divisor == 1 else number / divisor
    if characteristic == uuid("2a04"):
        code = payload[12]
        values["operating_mode_code"] = chr(code) if 32 <= code < 127 else f"0x{code:02x}"
        values["event_flags_hex"] = payload[8:12].hex()
        if values["battery_capacity_percent"] > 100:
            raise ValueError("battery capacity exceeds 100%; protocol may differ")
    return values
