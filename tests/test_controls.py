from fixtures import ADDRESS, PIN, DEVICE_SUFFIX, MQTT_ID
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'spc_bluetooth'))
from controls import ControlSession, decode_settings, prepare_settings, settings_path, SETTINGS_UUID
from decoder import SERVICE
from poller import wait_for_poll, connection, DEVICE
from dbus_fast import Variant
import tempfile
import json

CAPTURE = bytes.fromhex('e600f4011e021c024002a401cc011c0200020302')


class ControlsTests(unittest.IsolatedAsyncioTestCase):
    def test_output_and_charger_change_only_the_authorized_byte(self):
        for key, choices, offset in [('output_source_priority', ['USB','SUB','SBU'],17),
                                     ('charger_source_priority',['Solar only','Solar and utility'],18)]:
            for option in choices:
                planned = prepare_settings(CAPTURE, key, option)
                self.assertEqual(decode_settings(planned)[key], option)
                self.assertEqual(planned[:offset]+planned[offset+1:], CAPTURE[:offset]+CAPTURE[offset+1:])
        for bad in [CAPTURE[:-1],CAPTURE+b'\x00',CAPTURE[:17]+b'\xff'+CAPTURE[18:]]:
            with self.assertRaises(ValueError): decode_settings(bad)
        with self.assertRaises(ValueError): prepare_settings(CAPTURE,'charger_source_priority','Utility first')

    def test_selects_correct_service_and_requires_write_response(self):
        row = {'service':SERVICE,'uuid':SETTINGS_UUID,'flags':['read','write'],'path':'/settings'}
        self.assertEqual(settings_path([row]),'/settings')
        for rows in ([],[row,row],[{**row,'service':'other'}],[{**row,'flags':['read','write-without-response']}]):
            with self.assertRaises(ValueError): settings_path(rows)

    async def test_refresh_and_already_selected_never_write(self):
        request = AsyncMock(return_value=[CAPTURE]); pub=MagicMock(); reports=[]
        session=ControlSession(None,'/settings',request,pub,lambda r: reports.append(deepcopy(r)))
        await session.refresh()
        await session.execute('output_source_priority','SBU')
        self.assertTrue(all(c.args[3]=='ReadValue' for c in request.call_args_list))
        self.assertEqual(reports[-1]['status'],'already_selected')
        pub.publish_controls.assert_called_with({'output_source_priority':'SBU','charger_source_priority':'Solar only'})

    async def test_verified_charger_change_preserves_output_and_limits(self):
        planned = prepare_settings(CAPTURE,'charger_source_priority','Solar and utility')
        request=AsyncMock(side_effect=[[CAPTURE],[CAPTURE],[],[planned]])
        pub=MagicMock(); reports=[]
        session=ControlSession(None,'/settings',request,pub,lambda r: reports.append(deepcopy(r)))
        with patch('controls.asyncio.sleep',AsyncMock()):
            await session.execute('charger_source_priority','Solar and utility')
        writes=[c for c in request.call_args_list if c.args[3]=='WriteValue']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0].args[5][0],planned)
        self.assertEqual(writes[0].args[5][1]['type'].value,'request')
        self.assertEqual(reports[1]['status'],'write_unconfirmed')
        self.assertEqual(reports[-1]['changed_byte_offsets'],[18])
        self.assertEqual(reports[-1]['status'],'confirmed')
        pub.publish_controls.assert_called_once_with({'output_source_priority':'SBU','charger_source_priority':'Solar and utility'})

    async def test_changed_baseline_aborts(self):
        other=prepare_settings(CAPTURE,'output_source_priority','USB')
        request=AsyncMock(side_effect=[[CAPTURE],[other]]); pub=MagicMock(); reports=[]
        session=ControlSession(None,'/settings',request,pub,lambda r: reports.append(deepcopy(r)))
        with self.assertRaisesRegex(RuntimeError,'Settings changed'):
            await session.execute('charger_source_priority','Solar and utility')
        self.assertTrue(all(c.args[3]=='ReadValue' for c in request.call_args_list))
        pub.publish_controls.assert_not_called()
        self.assertFalse(reports[-1]['write_attempted'])

    async def test_timeout_and_unrelated_changes_never_retry_or_publish_success(self):
        other=bytearray(prepare_settings(CAPTURE,'output_source_priority','USB'));other[18]=2
        for replies in ([[CAPTURE],[CAPTURE],TimeoutError('timeout')],
                        [[CAPTURE],[CAPTURE],[],[bytes(other)]],
                        [[CAPTURE],[CAPTURE],[],[CAPTURE],[CAPTURE],[CAPTURE]]):
            with self.subTest(replies=replies):
                request=AsyncMock(side_effect=replies);pub=MagicMock();reports=[]
                session=ControlSession(None,'/settings',request,pub,lambda r: reports.append(deepcopy(r)))
                with patch('controls.asyncio.sleep',AsyncMock()), self.assertRaises(RuntimeError):
                    await session.execute('output_source_priority','USB')
                self.assertEqual(sum(c.args[3]=='WriteValue' for c in request.call_args_list),1)
                pub.publish_controls.assert_not_called()
                self.assertEqual(reports[-1]['status'],'unconfirmed')

    async def test_wait_loop_processes_a_command_without_waiting_for_next_measurement(self):
        pub=MagicMock();pub.take_command.return_value=('output_source_priority','SBU')
        controller=MagicMock();controller.execute=AsyncMock()
        await wait_for_poll(0,controller,pub)
        controller.execute.assert_awaited_once_with('output_source_priority','SBU')


class ConnectionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_routes_live_mqtt_command_through_verified_settings_write(self):
        from mqtt_bridge import Publisher
        target=f'/org/bluez/hci0/dev_{DEVICE_SUFFIX}'
        tree={target:{DEVICE:{'Paired':Variant('b',True),'Connected':Variant('b',True),
                              'ServicesResolved':Variant('b',True)}}}
        bus=MagicMock();bus.connect=AsyncMock()
        pub=Publisher(ADDRESS,allow_control=True)
        pub.client=MagicMock();pub.client.is_connected.return_value=True
        planned=prepare_settings(CAPTURE,'charger_source_priority','Solar and utility')
        read_count=0
        async def request(bus,path,interface,member,signature='',body=None,**kwargs):
            nonlocal read_count
            if member=='ReadValue':
                read_count+=1
                return [CAPTURE if read_count<=3 else planned]
            return []
        request_mock=AsyncMock(side_effect=request)
        async def command_loop(seconds,controller,publisher):
            message=MagicMock(topic=f'{pub.base}/charger_source_priority/set',
                              payload=b'Solar and utility',retain=False)
            pub.on_message(pub.client,None,message)
            command=pub.take_command()
            self.assertEqual(command,('charger_source_priority','Solar and utility'))
            await controller.execute(*command)
            raise RuntimeError('end of test connection')
        options={'address':ADDRESS,'adapter':'hci0','pin':PIN,'pair':True,
                 'diagnostics':False,'once':False,'allow_control':True,'poll_seconds':20}
        result={'timestamp':'2026-10-12T09:00:00+00:00','status':'ok','metrics':{'pv_power_w':0},'errors':[]}
        rows=[{'service':SERVICE,'uuid':SETTINGS_UUID,'flags':['read','write'],'path':target+'/settings'}]
        with tempfile.TemporaryDirectory() as directory:
            store=MagicMock();store.directory=Path(directory)
            with patch('poller.MessageBus',return_value=bus), patch('poller.discover',AsyncMock()), \
                 patch('poller.objects',AsyncMock(return_value=tree)), patch('poller.inventory',return_value=rows), \
                 patch('poller.sample',AsyncMock(return_value=result)), patch('poller.call',request_mock), \
                 patch('poller.wait_for_poll',command_loop), patch('controls.asyncio.sleep',AsyncMock()):
                with self.assertRaisesRegex(RuntimeError,'end of test'):
                    await connection(options,store,pub)
            report=json.loads((Path(directory)/'last-command.json').read_text())
            self.assertEqual(report['status'],'confirmed')
            self.assertEqual(report['changed_byte_offsets'],[18])
        writes=[c for c in request_mock.call_args_list if c.args[3]=='WriteValue']
        self.assertEqual(len(writes),1)
        self.assertEqual(writes[0].args[5][0],planned)
        self.assertIsNone(pub.take_command())
        self.assertIsNone(pub.control_seen)
        published={(c.args[0],c.args[1]) for c in pub.client.publish.call_args_list}
        self.assertIn((f'{pub.base}/output_source_priority/state','SBU'),published)
        self.assertIn((f'{pub.base}/charger_source_priority/state','Solar and utility'),published)
