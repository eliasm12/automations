from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'spc_bluetooth'))
from fixtures import ADDRESS, PIN
from poller import settings
import priority_test


class ConfigurationTests(unittest.TestCase):
    def test_poller_requires_configured_address_and_pin(self):
        with patch('sys.argv', ['poller.py']), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            settings()
        self.assertEqual(error.exception.code, 2)
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'options.json'
            file.write_text(json.dumps({'address':ADDRESS}))
            with patch('sys.argv',['poller.py','--options',str(file)]), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                settings()
            file.write_text(json.dumps({'address':ADDRESS,'pin':PIN,'adapter':'hci1'}))
            with patch('sys.argv',['poller.py','--options',str(file)]):
                loaded=settings()
            self.assertEqual(loaded['address'],ADDRESS)
            self.assertEqual(loaded['pin'],PIN)
            self.assertEqual(loaded['adapter'],'hci1')

    def test_priority_script_uses_config_with_explicit_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'options.json'
            file.write_text(json.dumps({'address':ADDRESS,'adapter':'hci1','pin':PIN}))
            args=['priority_test.py','--options',str(file),'--adapter','hci2','--mode','SBU',
                  '--output-dir',str(Path(directory)/'results')]
            mock=AsyncMock(return_value=0)
            with patch('sys.argv',args), patch('priority_test.run',mock), redirect_stdout(StringIO()):
                self.assertEqual(priority_test.main(),0)
            passed=mock.call_args.args[0]
            self.assertEqual(passed.address,ADDRESS)
            self.assertEqual(passed.adapter,'hci2')
            self.assertFalse(passed.apply)

    def test_priority_script_has_no_default_device_and_never_connects_without_configuration(self):
        mock=AsyncMock()
        with patch('sys.argv',['priority_test.py']), patch('priority_test.run',mock), \
             redirect_stderr(StringIO()), self.assertRaises(SystemExit) as error:
            priority_test.main()
        self.assertEqual(error.exception.code,2)
        mock.assert_not_called()

    def test_example_has_blank_connection_details(self):
        example=json.loads((Path(__file__).resolve().parents[1]/'options.example.json').read_text())
        self.assertEqual(example['address'],'')
        self.assertEqual(example['pin'],'')
