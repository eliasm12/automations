"""Verified settings-block writes, serialized on the poller's BlueZ connection."""
import asyncio
from datetime import datetime, timezone
import json
from dbus_fast import Variant
from decoder import SERVICE, uuid

SETTINGS_UUID = uuid('2a0c')
SELECTS = {
    'output_source_priority': ('Output source priority', 17, {'USB': 0, 'SUB': 1, 'SBU': 2}),
    'charger_source_priority': ('Battery charging source', 18,
                                {'Solar only': 3, 'Solar and utility': 2}),
}
# Read all recognized settings, but expose only the two requested charging choices.
CHARGERS = {0: 'Utility first', 1: 'Solar first', 2: 'Solar and utility', 3: 'Solar only'}


def decode_settings(payload):
    if len(payload) != 20:
        raise ValueError(f'Expected exactly 20 settings bytes, got {len(payload)}')
    if payload[17] not in (0, 1, 2) or payload[18] not in CHARGERS:
        raise ValueError('Unrecognized priority codes; refusing settings layout')
    return {'output_source_priority': ('USB', 'SUB', 'SBU')[payload[17]],
            'charger_source_priority': CHARGERS[payload[18]]}


def prepare_settings(before, key, option):
    decode_settings(before)
    if key not in SELECTS or option not in SELECTS[key][2]:
        raise ValueError('Unsupported priority selection')
    planned = bytearray(before)
    planned[SELECTS[key][1]] = SELECTS[key][2][option]
    return bytes(planned)


def settings_path(rows):
    matches = [r for r in rows if r['service'] == SERVICE and r['uuid'] == SETTINGS_UUID]
    if len(matches) != 1 or not {'read', 'write'}.issubset(matches[0]['flags']):
        raise ValueError('Expected one readable/writable settings characteristic in service 1810')
    return matches[0]['path']


class ControlSession:
    def __init__(self, bus, path, request, publisher, save):
        self.bus, self.path, self.request = bus, path, request
        self.publisher, self.save = publisher, save

    async def read(self):
        response = await self.request(self.bus, self.path, 'org.bluez.GattCharacteristic1',
                                      'ReadValue', 'a{sv}', [{}])
        payload = bytes(response[0])
        decode_settings(payload)
        return payload

    async def refresh(self):
        payload = await self.read()
        self.publisher.publish_controls(decode_settings(payload))
        return payload

    async def execute(self, key, option):
        # Do not accept further commands while a write/readback is in progress.
        self.publisher.disable_controls()
        report = {'timestamp': datetime.now(timezone.utc).isoformat(),
                  'key': key, 'requested': option, 'status': 'reading', 'write_attempted': False}
        self.save(report)
        try:
            before = await self.read()
            planned = prepare_settings(before, key, option)
            report.update(before=before.hex(), planned=planned.hex())
            if before == planned:
                report['status'] = 'already_selected'
                self.save(report)
                self.publisher.publish_controls(decode_settings(before))
                return
            if await self.read() != before:
                raise RuntimeError('Settings changed before write; no command sent')
            report.update(status='write_unconfirmed', write_attempted=True)
            self.save(report)  # Save original settings before touching hardware.
            await self.request(self.bus, self.path, 'org.bluez.GattCharacteristic1',
                               'WriteValue', 'aya{sv}',
                               [planned, {'type': Variant('s', 'request')}], timeout=10)
            report['gatt_acknowledged'] = True
            self.save(report)
            for delay in (1, 2, 3):
                await asyncio.sleep(delay)
                after = await self.read()
                report['after'] = after.hex()
                changed = [i for i in range(20) if before[i] != after[i]]
                report['changed_byte_offsets'] = changed
                if any(i != SELECTS[key][1] for i in changed):
                    raise RuntimeError('Unrelated settings changed; inspect last-command.json and the inverter')
                if after == planned:
                    report['status'] = 'confirmed'
                    self.save(report)
                    self.publisher.publish_controls(decode_settings(after))
                    return
            raise RuntimeError('Requested setting did not match readback')
        except Exception as error:
            report['status'] = 'unconfirmed' if report['write_attempted'] else 'failed_before_write'
            report['error'] = f'{type(error).__name__}: {error}'
            self.save(report)
            # Neither retry nor automatic rollback: an acknowledgement may be lost.
            raise RuntimeError(f'Priority command failed: {report["error"]}') from error
