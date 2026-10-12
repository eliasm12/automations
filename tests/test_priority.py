from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'spc_bluetooth'))
from decoder import SERVICE
from priority_test import PRIORITY_UUID, attempt, planned_settings, select_characteristic

CAPTURE = bytes.fromhex('e600f4011e021c024002a401cc011c0200020302')


class PriorityTests(unittest.IsolatedAsyncioTestCase):
    def test_plan_preserves_other_settings(self):
        for mode, code in [('USB', 0), ('SUB', 1), ('SBU', 2)]:
            planned = planned_settings(CAPTURE, mode)
            self.assertEqual(planned[17], code)
            self.assertEqual(planned[:17] + planned[18:], CAPTURE[:17] + CAPTURE[18:])
            self.assertEqual(planned[18], 3)
        for invalid in (CAPTURE[:-1], CAPTURE + b'\x00', CAPTURE[:17] + b'\xff' + CAPTURE[18:]):
            with self.assertRaises(ValueError):
                planned_settings(invalid, 'USB')
        with self.assertRaises(ValueError):
            planned_settings(CAPTURE, 'invalid')

    def test_only_expected_writable_characteristic_is_eligible(self):
        row = {'service': SERVICE, 'uuid': PRIORITY_UUID, 'flags': ['read', 'write'], 'path': '/expected'}
        self.assertEqual(select_characteristic([row]), '/expected')
        for rows in ([], [row, row], [{**row, 'service': 'other'}], [{**row, 'flags': ['read']}],
                     [{**row, 'flags': ['read', 'write-without-response']}]):
            with self.assertRaises(ValueError):
                select_characteristic(rows)

    async def test_preview_and_noop_never_write(self):
        for mode, apply in [(None, False), ('USB', False), ('SBU', True)]:
            with self.subTest(mode=mode, apply=apply):
                report = {}
                with patch('priority_test.call', AsyncMock(return_value=[CAPTURE])) as call:
                    self.assertEqual(await attempt(None, '/expected', mode, apply, report, lambda r: None), 0)
                self.assertTrue(all(c.args[3] == 'ReadValue' for c in call.call_args_list))

    async def test_external_change_aborts_before_write(self):
        different = bytearray(CAPTURE)
        different[18] = 2
        report = {}
        with patch('priority_test.call', AsyncMock(side_effect=[[CAPTURE], [bytes(different)]])) as call:
            with self.assertRaisesRegex(RuntimeError, 'Settings changed'):
                await attempt(None, '/expected', 'USB', True, report, lambda r: None)
        self.assertTrue(all(c.args[3] == 'ReadValue' for c in call.call_args_list))

    async def test_success_requires_exact_readback(self):
        planned = planned_settings(CAPTURE, 'USB')
        snapshots = []
        report = {}
        with patch('priority_test.call', AsyncMock(side_effect=[[CAPTURE], [CAPTURE], [], [planned]])) as call, \
             patch('priority_test.asyncio.sleep', AsyncMock()):
            self.assertEqual(await attempt(None, '/expected', 'USB', True, report,
                                           lambda r: snapshots.append(dict(r))), 0)
        writes = [c for c in call.call_args_list if c.args[3] == 'WriteValue']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args[4], 'aya{sv}')
        self.assertEqual(writes[0].args[5][0], planned)
        self.assertEqual(writes[0].args[5][1]['type'].value, 'request')
        self.assertIn('write_attempt_unconfirmed', [r['status'] for r in snapshots])
        self.assertEqual(report['status'], 'readback_matches_candidate')
        self.assertEqual(report['changed_byte_offsets'], [17])

    async def test_timeout_never_retries_write(self):
        report = {}
        with patch('priority_test.call', AsyncMock(side_effect=[[CAPTURE], [CAPTURE], TimeoutError('timed out')])) as call:
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                await attempt(None, '/expected', 'USB', True, report, lambda r: None)
        self.assertEqual(sum(c.args[3] == 'WriteValue' for c in call.call_args_list), 1)
        self.assertEqual(report['status'], 'write_unconfirmed')

    async def test_collateral_change_does_not_report_success_or_rollback(self):
        different = bytearray(planned_settings(CAPTURE, 'USB'))
        different[18] = 0
        report = {}
        with patch('priority_test.call', AsyncMock(side_effect=[[CAPTURE], [CAPTURE], [], [bytes(different)]])) as call, \
             patch('priority_test.asyncio.sleep', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'Other settings changed'):
                await attempt(None, '/expected', 'USB', True, report, lambda r: None)
        self.assertEqual(sum(c.args[3] == 'WriteValue' for c in call.call_args_list), 1)
        self.assertEqual(report['status'], 'write_unconfirmed')
