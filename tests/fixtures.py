"""Synthetic connection identifiers; never use a real device or credential."""
ADDRESS = ':'.join(f'{part:02X}' for part in (2, 0, 0, 0, 0, 1))
PIN = ''.join(str(part) for part in range(6))
DEVICE_SUFFIX = ADDRESS.replace(':', '_')
MQTT_ID = ADDRESS.lower().replace(':', '')
