#!/usr/bin/env python3
"""Experimental standalone output-priority test; never run by the HA app.

2a0c accepted 20-byte settings blocks with only byte 17 changed for USB,
SBU, and SUB on this inverter on 2026-10-12, verified by readback and
reconnect. USB was also confirmed on its display. Other firmware variants
still require hardware verification.
Default operation reads and previews only. --apply attempts one write.
"""
import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from decoder import SERVICE, uuid
from poller import CHAR, DEVICE, atomic_json, call, discover, inventory, objects, prop

PRIORITY_UUID = uuid('2a0c')
MODES = {'USB': 0, 'SUB': 1, 'SBU': 2}
CHARGERS = {0: 'Utility first', 1: 'Solar first', 2: 'Solar and utility', 3: 'Solar only'}


def describe(payload):
    if len(payload) != 20:
        raise ValueError(f'Expected exactly 20 settings bytes, got {len(payload)}')
    if payload[17] not in MODES.values() or payload[18] not in CHARGERS:
        raise ValueError('Unrecognized source-priority codes; refusing this layout')
    return {'output_priority': next(k for k, v in MODES.items() if v == payload[17]),
            'charger_priority': CHARGERS[payload[18]], 'hex': payload.hex()}


def planned_settings(before, mode):
    describe(before)
    if mode not in MODES:
        raise ValueError('Priority must be USB, SUB, or SBU')
    planned = bytearray(before)
    planned[17] = MODES[mode]
    return bytes(planned)


def select_characteristic(rows):
    matches = [row for row in rows if row['service'] == SERVICE and row['uuid'] == PRIORITY_UUID]
    if len(matches) != 1:
        raise ValueError('Expected one settings characteristic under service 1810')
    row = matches[0]
    if not {'read', 'write'}.issubset(row['flags']):
        raise ValueError('Settings characteristic must support read and write with response')
    return row['path']


async def read_settings(bus, path):
    response = await call(bus, path, CHAR, 'ReadValue', 'a{sv}', [{}])
    payload = bytes(response[0])
    describe(payload)
    return payload


async def attempt(bus, path, mode, apply, report, save):
    before = await read_settings(bus, path)
    report['before'] = describe(before)
    report['status'] = 'read_only'
    save(report)
    print('Current:', json.dumps(report['before']), flush=True)
    if mode is None:
        return 0
    planned = planned_settings(before, mode)
    report['planned'] = describe(planned)
    print('Planned:', json.dumps(report['planned']), flush=True)
    if not apply:
        report['status'] = 'preview_only'
        save(report)
        print('Preview only; no write sent.', flush=True)
        return 0
    if planned == before:
        report['status'] = 'already_selected_no_write'
        save(report)
        print('Already selected; no write sent.', flush=True)
        return 0
    # Detect external setting changes immediately before the single attempt.
    if await read_settings(bus, path) != before:
        raise RuntimeError('Settings changed since preview; aborting without a write')
    report['status'] = 'write_attempt_unconfirmed'
    save(report)  # Persist the original and intended bytes BEFORE touching hardware.
    print('Attempting ONE experimental whole-block write. No automatic retry or rollback.', flush=True)
    try:
        await call(bus, path, CHAR, 'WriteValue', 'aya{sv}',
                   [planned, {'type': Variant('s', 'request')}], timeout=10)
        # A GATT acknowledgement alone does not confirm the inverter accepted it.
        report['gatt_acknowledged'] = True
        save(report)
        for delay in (1, 2, 3):
            await asyncio.sleep(delay)
            after = await read_settings(bus, path)
            report['after'] = describe(after)
            changed = [i for i in range(20) if before[i] != after[i]]
            report['changed_byte_offsets'] = changed
            if any(i != 17 for i in changed):
                raise RuntimeError('Other settings changed; inspect the inverter display and saved baseline')
            if after == planned:
                report['status'] = 'readback_matches_candidate'
                report['readback_verified'] = True
                save(report)
                print('Readback matches; only byte 17 changed. Confirm on the inverter display.', flush=True)
                return 0
        raise RuntimeError('Priority did not change in readback; candidate format not confirmed')
    except Exception as error:
        report['status'] = 'write_unconfirmed'
        report['error'] = str(error)
        save(report)
        raise RuntimeError(f'Write outcome unconfirmed: {error}. No retry sent; check the inverter display.') from error


async def run(args, report, save):
    bus = MessageBus(bus_type=BusType.SYSTEM)
    adapter = f'/org/bluez/{args.adapter}'
    target = adapter + '/dev_' + args.address.replace(':', '_')
    connect_attempted = False
    try:
        report['phase'] = 'system_bus'
        await asyncio.wait_for(bus.connect(), 15)
        report['phase'] = 'device_discovery'
        await discover(bus, adapter, target)
        tree = await objects(bus)
        if prop(tree.get(target, {}), DEVICE, 'Connected', False):
            raise RuntimeError('Inverter is already connected on this adapter; stop the other reader first')
        if not prop(tree.get(target, {}), DEVICE, 'Paired', False):
            raise RuntimeError('Laptop is not paired; first run poller.py --once --diagnostics to pair')
        connect_attempted = True
        report['phase'] = 'bluetooth_connect'
        save(report)
        print('Connecting to inverter...', flush=True)
        await call(bus, target, DEVICE, 'Connect', timeout=30)
        report['phase'] = 'service_discovery'
        for _ in range(30):
            tree = await objects(bus)
            if prop(tree.get(target, {}), DEVICE, 'ServicesResolved', False):
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError('Bluetooth service discovery timed out')
        path = select_characteristic(inventory(tree, target))
        report['characteristic'] = path
        report['phase'] = 'settings_test'
        return await attempt(bus, path, args.mode, args.apply, report, save)
    finally:
        if connect_attempted:
            with suppress(Exception):
                await call(bus, target, DEVICE, 'Disconnect', timeout=5)
        bus.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--options', type=Path, help='Local JSON configuration with address and adapter')
    parser.add_argument('--address', help='Override the configured address')
    parser.add_argument('--adapter', help='Override the configured adapter')
    parser.add_argument('--mode', choices=list(MODES))
    parser.add_argument('--apply', action='store_true', help='Attempt one EXPERIMENTAL write; otherwise read/preview only')
    parser.add_argument('--output-dir', type=Path, default=Path('priority-test-results'))
    args = parser.parse_args()
    config = {}
    if args.options:
        try:
            config = json.loads(args.options.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            parser.error('Cannot load the --options JSON configuration')
        if not isinstance(config, dict):
            parser.error('--options must contain a JSON object')
    args.address = args.address if args.address is not None else config.get('address', '')
    args.adapter = args.adapter if args.adapter is not None else config.get('adapter', 'hci0')
    if not isinstance(args.address, str) or not args.address:
        parser.error('Set address in --options JSON or supply --address')
    if not isinstance(args.adapter, str):
        parser.error('Invalid adapter name')
    args.address = args.address.upper()
    if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', args.address):
        parser.error('Invalid Bluetooth address')
    if not re.fullmatch(r'hci\d+', args.adapter):
        parser.error('Invalid adapter name')
    if args.apply and not args.mode:
        parser.error('--apply requires --mode')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    file = args.output_dir / (now.strftime('priority-%Y%m%dT%H%M%S-%fZ') + '.json')
    report = {'timestamp': now.isoformat(), 'address': args.address, 'mode_requested': args.mode,
              'apply_requested': args.apply, 'write_format_verified': False, 'status': 'starting'}
    save = lambda value: atomic_json(file, value)
    save(report)
    print('Report:', file, flush=True)
    try:
        return asyncio.run(run(args, report, save))
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        if report['status'] != 'write_unconfirmed':
            report['status'] = 'failed_before_write' if report['status'] != 'write_attempt_unconfirmed' else 'write_unconfirmed'
        save(report)
        print(f"FAILED during {report.get('phase', 'startup')}: {report['error']}", flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
